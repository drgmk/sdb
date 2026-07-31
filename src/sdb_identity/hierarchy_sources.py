"""Acquisition and persistence of WDS/CCDM hierarchy source snapshots."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from .cache_store import SnapshotCache
from .hierarchy_parsing import parse_cached_snapshot, parse_snapshot, parse_tables
from .hierarchy_records import ParsedHierarchyRecord
from .hierarchy_registry import hierarchy_source
from .models import (
    HierarchyMatchAction,
    HierarchyMatchCandidate,
    HierarchyRecord,
    HierarchySource,
    StructuralEdge,
    StructuralEdgeAction,
    utcnow,
)
from .providers import ProviderError
from .snapshots import SnapshotClient, VizierSnapshotClient


@dataclass(frozen=True)
class HierarchyImportResult:
    source_id: int
    provider: str
    release: str
    row_count: int
    skipped_count: int
    checksum: str


@dataclass(frozen=True)
class HierarchyPruneResult:
    provider: str | None
    groups: int
    removed_sources: int
    removed_records: int
    removed_candidates: int
    removed_match_actions: int
    removed_graph_edges: int
    removed_graph_overrides: int


@dataclass(frozen=True)
class _AcquiredSnapshot:
    records: list[ParsedHierarchyRecord]
    readme: str
    source_url: str
    checksum: str
    release: str
    cache_note: str | None


class HierarchySourceService:
    """Own hierarchy snapshot transport, deduplication, and row persistence."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def import_snapshot(
        self,
        provider: str,
        path: str | Path,
        *,
        release: str,
        note: str | None = None,
    ) -> HierarchyImportResult:
        provider = provider.lower().strip()
        hierarchy_source(provider)
        if not release.strip():
            raise ValueError("release is required")
        path = Path(path).expanduser().resolve()
        data = path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        text_data = gzip.decompress(data) if path.suffix == ".gz" else data
        parsed, skipped = parse_snapshot(
            provider, text_data.decode("utf-8", errors="replace"),
        )
        with self.session_factory.begin() as session:
            existing = _existing_source_result(
                session, provider, checksum, skipped_count=skipped,
            )
            if existing is not None:
                return existing
            source = HierarchySource(
                provider=provider,
                release=release.strip(),
                source_file=str(path),
                checksum=checksum,
                note=note,
            )
            session.add(source)
            session.flush()
            _store_records(session, source, parsed)
            return HierarchyImportResult(
                source_id=source.id,
                provider=provider,
                release=source.release,
                row_count=len(parsed),
                skipped_count=skipped,
                checksum=checksum,
            )

    def fetch_snapshot(
        self,
        provider: str,
        *,
        client: SnapshotClient | None = None,
        cache_path: str | Path | None = None,
        refresh_cache: bool = False,
        release: str | None = None,
        note: str | None = None,
    ) -> HierarchyImportResult:
        provider = provider.lower().strip()
        catalog = hierarchy_source(provider).catalog
        client = client or VizierSnapshotClient()
        if cache_path is not None:
            acquired = self._fetch_cached(
                provider,
                catalog,
                client,
                cache_path,
                refresh_cache=refresh_cache,
                release=release,
                note=note,
            )
        else:
            acquired = self._fetch_direct(
                provider, catalog, client, release=release,
            )
        if not acquired.records:
            raise ProviderError(
                f"{provider} hierarchy snapshot returned no parseable rows",
            )
        with self.session_factory.begin() as session:
            existing = _existing_source_result(
                session, provider, acquired.checksum, skipped_count=0,
            )
            if existing is not None:
                return existing
            source = HierarchySource(
                provider=provider,
                release=acquired.release,
                source_file=acquired.source_url,
                checksum=acquired.checksum,
                fetched_at=utcnow(),
                note=_join_notes(
                    note,
                    _readme_version_note(acquired.readme),
                    acquired.cache_note,
                ),
            )
            session.add(source)
            session.flush()
            _store_records(session, source, acquired.records)
            return HierarchyImportResult(
                source_id=source.id,
                provider=provider,
                release=source.release,
                row_count=len(acquired.records),
                skipped_count=0,
                checksum=acquired.checksum,
            )

    def sources(self, provider: str | None = None) -> tuple[HierarchySource, ...]:
        with self.session_factory() as session:
            query = select(HierarchySource).order_by(HierarchySource.id)
            if provider is not None:
                query = query.where(
                    HierarchySource.provider == provider.lower().strip(),
                )
            return tuple(session.scalars(query))

    def prune_duplicate_sources(
        self, provider: str | None = None,
    ) -> HierarchyPruneResult:
        provider_value = None if provider is None else provider.lower().strip()
        if (
            provider_value is not None
            and provider_value not in {"wds", "ccdm", "simbad", "manual"}
        ):
            raise ValueError(f"unsupported hierarchy provider: {provider}")
        with self.session_factory.begin() as session:
            group_query = (
                select(HierarchySource.provider, HierarchySource.checksum)
                .where(HierarchySource.checksum.is_not(None))
                .group_by(HierarchySource.provider, HierarchySource.checksum)
                .having(func.count(HierarchySource.id) > 1)
                .order_by(HierarchySource.provider, HierarchySource.checksum)
            )
            if provider_value is not None:
                group_query = group_query.where(
                    HierarchySource.provider == provider_value,
                )
            groups = list(session.execute(group_query))
            remove_source_ids = _duplicate_source_ids(session, groups)
            if not remove_source_ids:
                return HierarchyPruneResult(
                    provider_value, 0, 0, 0, 0, 0, 0, 0,
                )

            record_ids = list(session.scalars(
                select(HierarchyRecord.id).where(
                    HierarchyRecord.source_id.in_(remove_source_ids),
                ),
            ))
            candidate_ids = _select_ids_by_chunks(
                session,
                select(HierarchyMatchCandidate.id),
                HierarchyMatchCandidate.record_id,
                record_ids,
            )
            edge_ids = list(session.scalars(
                select(StructuralEdge.id).where(
                    StructuralEdge.source_id.in_(remove_source_ids),
                ),
            ))
            removed_match_actions = _delete_by_chunks(
                session, HierarchyMatchAction,
                HierarchyMatchAction.candidate_id, candidate_ids,
            )
            removed_candidates = _delete_by_chunks(
                session, HierarchyMatchCandidate,
                HierarchyMatchCandidate.id, candidate_ids,
            )
            removed_graph_overrides = _delete_by_chunks(
                session, StructuralEdgeAction,
                StructuralEdgeAction.edge_id, edge_ids,
            )
            removed_graph_edges = _delete_by_chunks(
                session, StructuralEdge, StructuralEdge.id, edge_ids,
            )
            removed_records = _delete_by_chunks(
                session, HierarchyRecord, HierarchyRecord.id, record_ids,
            )
            removed_sources = _delete_by_chunks(
                session, HierarchySource, HierarchySource.id, remove_source_ids,
            )
            return HierarchyPruneResult(
                provider=provider_value,
                groups=len(groups),
                removed_sources=removed_sources,
                removed_records=removed_records,
                removed_candidates=removed_candidates,
                removed_match_actions=removed_match_actions,
                removed_graph_edges=removed_graph_edges,
                removed_graph_overrides=removed_graph_overrides,
            )

    def _fetch_cached(
        self,
        provider: str,
        catalog: str,
        client: SnapshotClient,
        cache_path: str | Path,
        *,
        refresh_cache: bool,
        release: str | None,
        note: str | None,
    ) -> _AcquiredSnapshot:
        cache = SnapshotCache(cache_path)
        cached = None if refresh_cache else cache.current_snapshot("vizier", catalog)
        cache_status = "reused"
        if cached is None:
            tables, readme = _fetch_upstream(provider, catalog, client)
            cached = cache.store_snapshot(
                provider="vizier",
                catalog_id=catalog,
                release=release or _release_from_readme(provider, catalog, readme),
                source_url=client.source_url(catalog),
                readme=readme,
                tables=tables,
                note=note,
            )
            cache_status = "stored"
        return _AcquiredSnapshot(
            records=parse_cached_snapshot(provider, cached),
            readme=cached.readme,
            source_url=cached.source_url,
            checksum=cached.content_sha256,
            release=release or cached.release,
            cache_note=(
                f"cache_source_id={cached.source_id};cache_status={cache_status}"
            ),
        )

    def _fetch_direct(
        self,
        provider: str,
        catalog: str,
        client: SnapshotClient,
        *,
        release: str | None,
    ) -> _AcquiredSnapshot:
        tables, readme = _fetch_upstream(provider, catalog, client)
        parsed = parse_tables(provider, tables)
        if not parsed:
            raise ProviderError(
                f"{provider} hierarchy snapshot returned no parseable rows",
            )
        release_value = release or _release_from_readme(provider, catalog, readme)
        canonical = json.dumps({
            "catalog": catalog,
            "provider": provider,
            "readme": readme,
            "records": [asdict(record) for record in parsed],
        }, sort_keys=True, ensure_ascii=False)
        return _AcquiredSnapshot(
            records=parsed,
            readme=readme,
            source_url=client.source_url(catalog),
            checksum=hashlib.sha256(canonical.encode()).hexdigest(),
            release=release_value,
            cache_note=None,
        )


def _fetch_upstream(provider: str, catalog: str, client: SnapshotClient):
    try:
        return client.fetch_tables(catalog), client.fetch_readme(catalog)
    except Exception as error:
        raise ProviderError(
            f"{provider} hierarchy snapshot fetch failed: {error}",
            transient=True,
        ) from error


def _store_records(
    session: Session,
    source: HierarchySource,
    parsed: list[ParsedHierarchyRecord],
) -> None:
    for record in parsed:
        session.add(HierarchyRecord(
            source_id=source.id,
            provider=source.provider,
            native_id=record.native_id,
            component=record.component,
            discoverer_id=record.discoverer_id,
            ra_deg=record.ra_deg,
            dec_deg=record.dec_deg,
            first_epoch=record.first_epoch,
            last_epoch=record.last_epoch,
            measure_epoch=record.measure_epoch,
            separation_arcsec=record.separation_arcsec,
            pa_deg=record.pa_deg,
            magnitude_primary=record.magnitude_primary,
            magnitude_secondary=record.magnitude_secondary,
            delta_mag=record.delta_mag,
            raw_payload_json=json.dumps(record.raw_payload or {}, sort_keys=True),
        ))


def _existing_source_result(
    session: Session,
    provider: str,
    checksum: str | None,
    *,
    skipped_count: int,
) -> HierarchyImportResult | None:
    if checksum is None:
        return None
    source = session.scalar(
        select(HierarchySource)
        .where(
            HierarchySource.provider == provider,
            HierarchySource.checksum == checksum,
        )
        .order_by(HierarchySource.id)
        .limit(1)
    )
    if source is None:
        return None
    row_count = session.scalar(
        select(func.count(HierarchyRecord.id)).where(
            HierarchyRecord.source_id == source.id,
        ),
    ) or 0
    return HierarchyImportResult(
        source_id=source.id,
        provider=source.provider,
        release=source.release,
        row_count=int(row_count),
        skipped_count=skipped_count,
        checksum=checksum,
    )


def _duplicate_source_ids(session: Session, groups) -> list[int]:
    values: list[int] = []
    for provider, checksum in groups:
        sources = list(session.scalars(
            select(HierarchySource)
            .where(
                HierarchySource.provider == provider,
                HierarchySource.checksum == checksum,
            )
            .order_by(HierarchySource.id)
        ))
        values.extend(source.id for source in sources[1:])
    return values


def _delete_by_chunks(session: Session, model, column, ids: list[int]) -> int:
    removed = 0
    for chunk in _chunks(ids, 800):
        result = session.execute(delete(model).where(column.in_(chunk)))
        removed += int(result.rowcount or 0)
    return removed


def _select_ids_by_chunks(
    session: Session, query, column, ids: list[int],
) -> list[int]:
    values: list[int] = []
    for chunk in _chunks(ids, 800):
        values.extend(session.scalars(query.where(column.in_(chunk))))
    return values


def _release_from_readme(provider: str, catalog: str, readme: str) -> str:
    date_match = re.search(
        r"(?:version|updated|last\s+update|date)\D{0,30}"
        r"((?:19|20)\d{2}[-/][A-Za-z0-9]{1,3}[-/][A-Za-z0-9]{1,4}|"
        r"[A-Za-z]{3,9}\s+\d{1,2},?\s+(?:19|20)\d{2}|"
        r"(?:19|20)\d{2}\.\d+)",
        readme,
        flags=re.IGNORECASE,
    )
    if date_match:
        return f"{provider}:{catalog}:{date_match.group(1).strip()}"
    digest = hashlib.sha256(readme.encode()).hexdigest()[:12]
    return f"{provider}:{catalog}:readme-{digest}"


def _readme_version_note(readme: str) -> str | None:
    for line in readme.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ("version", "updated", "last update", "date")):
            return line.strip()
    return None


def _join_notes(*values: str | None) -> str | None:
    notes = [value.strip() for value in values if value and value.strip()]
    return "; ".join(notes) if notes else None


def _chunks(values: list[int], size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]
