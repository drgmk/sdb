from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .hierarchy import HierarchyService
from .hierarchy_semantics import simbad_component_relevance
from .ingestion import TargetIngestionPlan
from .models import ExternalIdentifier, MetadataRun, SimbadMetadata, Target
from .providers import Astrometry, SimbadDiscoveryProvider
from .identifiers import normalize_identifier
from .service import AddRequest, IdentityService, UnresolvedTarget
from .update import UpdateService, UpdateSummary
from .targets import resolve_target


@dataclass(frozen=True)
class NearbySimbadCandidate:
    oid: int
    main_id: str
    ra_deg: float
    dec_deg: float
    separation_arcsec: float
    primary_object_type: str | None
    object_type_label: str | None
    object_type_description: str | None
    object_types: tuple[str, ...]
    spectral_type: str | None
    component_relevance: str
    blocked_reason: str | None
    existing_target_id: int | None
    existing_sdbid: str | None
    current_target: bool

    @property
    def selectable(self) -> bool:
        return self.existing_target_id is None and self.blocked_reason is None

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["object_types"] = list(self.object_types)
        value["selectable"] = self.selectable
        return value


@dataclass(frozen=True)
class NearbySimbadSearch:
    target_id: int
    target_sdbid: str
    center_ra_deg: float
    center_dec_deg: float
    radius_arcsec: float
    candidates: tuple[NearbySimbadCandidate, ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["candidates"] = [
            candidate.as_dict() for candidate in self.candidates
        ]
        return value


@dataclass(frozen=True)
class TargetImportItem:
    requested_name: str
    status: str
    target_id: int | None = None
    sdbid: str | None = None
    astrometry_source: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class TargetImportResult:
    requested_count: int
    succeeded_count: int
    created_count: int
    existing_count: int
    failed_count: int
    providers: tuple[str, ...]
    items: tuple[TargetImportItem, ...]
    update_summary: UpdateSummary | None
    hierarchy_matches: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        value = {
            **asdict(self),
            "providers": list(self.providers),
            "items": [asdict(item) for item in self.items],
            "hierarchy_matches": list(self.hierarchy_matches),
        }
        value["update_summary"] = (
            None
            if self.update_summary is None
            else {
                **asdict(self.update_summary),
                "items": [
                    asdict(item) for item in self.update_summary.items
                ],
            }
        )
        return value


def search_nearby_simbad(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    provider: SimbadDiscoveryProvider,
    radius_arcsec: float = 60.0,
    limit: int = 100,
) -> NearbySimbadSearch:
    if radius_arcsec <= 0 or radius_arcsec > 600:
        raise ValueError("SIMBAD search radius must be between 0 and 600 arcsec")
    if limit < 1 or limit > 500:
        raise ValueError("SIMBAD search limit must be between 1 and 500")
    with session_factory() as session:
        target = resolve_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        target_id = target.id
        target_sdbid = target.sdbid
        center = Astrometry(
            target.ra2000_deg,
            target.dec2000_deg,
            source="sdb",
            source_id=target.sdbid,
        )
    neighbours = provider.search_region(
        center,
        radius_arcsec=radius_arcsec,
        limit=limit,
    )
    with session_factory() as session:
        existing_by_oid = _targets_by_simbad_oid(
            session, [value.oid for value in neighbours]
        )
        existing_by_identifier = _targets_by_identifier(
            session, [value.main_id for value in neighbours]
        )
        candidates = []
        for neighbour in neighbours:
            existing = (
                existing_by_oid.get(neighbour.oid)
                or existing_by_identifier.get(
                    normalize_identifier(neighbour.main_id)
                )
            )
            relevance = simbad_component_relevance(
                neighbour.primary_object_type,
                list(neighbour.object_types),
            )
            blocked_reason = {
                "planetary_or_disk": "planet",
                "contextual_group": "contextual group",
            }.get(relevance)
            candidates.append(NearbySimbadCandidate(
                oid=neighbour.oid,
                main_id=neighbour.main_id,
                ra_deg=neighbour.astrometry.ra_deg,
                dec_deg=neighbour.astrometry.dec_deg,
                separation_arcsec=neighbour.separation_arcsec,
                primary_object_type=neighbour.primary_object_type,
                object_type_label=neighbour.object_type_label,
                object_type_description=neighbour.object_type_description,
                object_types=neighbour.object_types,
                spectral_type=neighbour.spectral_type,
                component_relevance=relevance,
                blocked_reason=blocked_reason,
                existing_target_id=None if existing is None else existing.id,
                existing_sdbid=None if existing is None else existing.sdbid,
                current_target=(
                    existing is not None and existing.id == target_id
                ),
            ))
    return NearbySimbadSearch(
        target_id=target_id,
        target_sdbid=target_sdbid,
        center_ra_deg=center.ra_deg,
        center_dec_deg=center.dec_deg,
        radius_arcsec=radius_arcsec,
        candidates=tuple(sorted(
            candidates,
            key=lambda value: (value.separation_arcsec, value.main_id),
        )),
    )


class TargetImportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        identity_service: IdentityService,
        update_service: UpdateService,
        hierarchy_service: HierarchyService | None = None,
    ):
        self.sessions = session_factory
        self.identity = identity_service
        self.update = update_service
        self.hierarchy = hierarchy_service or HierarchyService(session_factory)
        self.plan = TargetIngestionPlan(
            identity=self.identity,
            update=self.update,
            hierarchy=self.hierarchy,
        )

    def import_many(
        self,
        names: Iterable[str],
        *,
        providers: Iterable[str],
        command: str = "target import",
        hierarchy_radius_arcsec: float = 30.0,
    ) -> TargetImportResult:
        requested = tuple(dict.fromkeys(
            str(name).strip() for name in names if str(name).strip()
        ))
        if not requested:
            raise ValueError("at least one target name is required")
        selected_providers = tuple(dict.fromkeys(
            str(provider).strip() for provider in providers
            if str(provider).strip()
        ))
        if not selected_providers:
            raise ValueError("at least one provider is required")
        items = []
        successful_sdbids = []
        for name in requested:
            try:
                added = self.plan.identify(AddRequest(
                    name=name,
                    command=f"{command}: {name}",
                ))
            except (UnresolvedTarget, ValueError, RuntimeError) as error:
                items.append(TargetImportItem(
                    requested_name=name,
                    status="failed",
                    error=str(error),
                ))
                continue
            items.append(TargetImportItem(
                requested_name=name,
                status="created" if added.created else "existing",
                target_id=added.target_id,
                sdbid=added.sdbid,
                astrometry_source=added.astrometry_source,
            ))
            successful_sdbids.append(added.sdbid)

        unique_sdbids = tuple(dict.fromkeys(successful_sdbids))
        followup = self.plan.follow_up(
            unique_sdbids,
            providers=selected_providers,
            hierarchy_radius_arcsec=hierarchy_radius_arcsec,
        )
        return TargetImportResult(
            requested_count=len(requested),
            succeeded_count=sum(item.status != "failed" for item in items),
            created_count=sum(item.status == "created" for item in items),
            existing_count=sum(item.status == "existing" for item in items),
            failed_count=sum(item.status == "failed" for item in items),
            providers=selected_providers,
            items=tuple(items),
            update_summary=followup.update_summary,
            hierarchy_matches=followup.hierarchy_matches,
        )


def _targets_by_simbad_oid(
    session: Session,
    oids: Iterable[int],
) -> dict[int, Target]:
    values = tuple(dict.fromkeys(int(value) for value in oids))
    if not values:
        return {}
    result = {}
    rows = session.execute(
        select(SimbadMetadata, Target)
        .join(MetadataRun, MetadataRun.id == SimbadMetadata.run_id)
        .join(Target, Target.id == SimbadMetadata.target_id)
        .where(
            SimbadMetadata.oid.in_(values),
            MetadataRun.is_current.is_(True),
        )
        .order_by(SimbadMetadata.oid, Target.sdbid)
    )
    for metadata, target in rows:
        result.setdefault(metadata.oid, target)
    return result


def _targets_by_identifier(
    session: Session,
    identifiers: Iterable[str],
) -> dict[str, Target]:
    values = tuple(dict.fromkeys(
        normalize_identifier(value) for value in identifiers
        if normalize_identifier(value)
    ))
    if not values:
        return {}
    result = {}
    rows = session.execute(
        select(ExternalIdentifier, Target)
        .join(Target, Target.id == ExternalIdentifier.target_id)
        .where(ExternalIdentifier.normalized_value.in_(values))
        .order_by(ExternalIdentifier.normalized_value, Target.sdbid)
    )
    for identifier, target in rows:
        result.setdefault(identifier.normalized_value, target)
    return result
