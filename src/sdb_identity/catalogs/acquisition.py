from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ..astrometry import propagate_to_epoch
from .types import (
    CatalogAdapter,
    CatalogCandidate,
    CatalogQueryContext,
    CatalogRefreshResult,
)
from .results import catalog_run_signature, effective_catalog_results
from .ingestion import (
    shared_detection_target_ids,
    store_catalog_attributes,
)
from .matching import match_catalog_candidates
from .detection_ingestion import DetectionIngestor
from ..dirty import mark_export_dirty
from ..models.identity import AstrometricSolution, ExternalIdentifier, Target
from ..models.catalogs import (
    CatalogBatchRequest,
    CatalogDetection,
    CatalogRun,
    RawCatalogRow,
)
from ..providers import Astrometry, ProviderError
from ..target_astrometry import (
    best_target_astrometry,
    best_target_astrometry_map,
)
from ..targets import resolve_target
from ..vocabulary import ProviderRunStatus


class CatalogAcquisitionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapters: Mapping[str, CatalogAdapter],
        *,
        acceptance_score: float = 0.5,
        acceptance_margin: float = 0.15,
        score_scale_arcsec: float = 2.0,
    ):
        self.sessions = session_factory
        self.adapters = dict(adapters)
        self.acceptance_score = acceptance_score
        self.acceptance_margin = acceptance_margin
        self.score_scale_arcsec = score_scale_arcsec

    def refresh(
        self,
        target_reference: str | int,
        provider: str,
        *,
        preloaded_candidates: list[CatalogCandidate] | None = None,
        batch_request_id: int | None = None,
    ) -> CatalogRefreshResult:
        if provider not in self.adapters:
            raise KeyError(f"unknown catalog provider: {provider}")
        adapter = self.adapters[provider]
        with self.sessions() as session:
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            solution = session.get(AstrometricSolution, target.canonical_astrometry_id)
            if solution is None:
                raise RuntimeError(f"target {target.sdbid} has no canonical astrometry")
            native = best_target_astrometry(session, target)
            query_astrometry = propagate_to_epoch(native, adapter.query_epoch)
            identifiers = tuple(session.scalars(
                select(ExternalIdentifier.value)
                .where(ExternalIdentifier.target_id == target.id)
                .order_by(ExternalIdentifier.id)
            ))
            context = CatalogQueryContext(
                target.id, target.sdbid, query_astrometry, identifiers
            )
            previous = session.scalar(select(CatalogRun).where(
                CatalogRun.target_id == target.id,
                CatalogRun.provider == adapter.name,
                CatalogRun.is_current.is_(True),
            ))
            previous_result = effective_catalog_results(
                session, [target.id], providers=(adapter.name,),
            ).get((target.id, adapter.name))
            previous_signature = catalog_run_signature(
                session,
                previous,
                effective_status=(
                    None
                    if previous_result is None
                    else previous_result.status
                ),
                selected_source_id=(
                    None
                    if previous_result is None
                    else previous_result.selected_source_id
                ),
                selected_raw_row_id=(
                    None
                    if previous_result is None
                    or previous_result.selected_raw_row is None
                    else previous_result.selected_raw_row.id
                ),
            )
            session.execute(
                update(CatalogRun)
                .where(
                    CatalogRun.target_id == target.id,
                    CatalogRun.provider == adapter.name,
                    CatalogRun.status == ProviderRunStatus.RUNNING,
                )
                .values(
                    status=ProviderRunStatus.TRANSIENT_FAILURE,
                    error="superseded after interrupted refresh",
                    completed_at=datetime.now(timezone.utc),
                    is_current=False,
                )
            )
            run = CatalogRun(
                target_id=target.id,
                batch_request_id=batch_request_id,
                provider=adapter.name,
                release=adapter.release,
                status=ProviderRunStatus.RUNNING,
                is_current=False,
                query_ra_deg=query_astrometry.ra_deg,
                query_dec_deg=query_astrometry.dec_deg,
                query_epoch=query_astrometry.epoch,
            )
            session.add(run)
            session.flush()
            # Do not hold a SQLite write transaction during a remote catalog
            # request. A crash leaves an inspectable running attempt.
            session.commit()
            try:
                candidates = (
                    adapter.query(context)
                    if preloaded_candidates is None
                    else preloaded_candidates
                )
            except ProviderError as error:
                run.status = (
                    ProviderRunStatus.TRANSIENT_FAILURE
                    if error.transient
                    else ProviderRunStatus.PERMANENT_FAILURE
                )
                run.error = str(error)
                run.completed_at = datetime.now(timezone.utc)
                session.commit()
                return CatalogRefreshResult(
                    run.id,
                    target.id,
                    provider,
                    ProviderRunStatus.parse(run.status, "catalog status"),
                    0,
                    0,
                    error=str(error),
                )

            match = match_catalog_candidates(
                adapter,
                context,
                candidates,
                acceptance_score=self.acceptance_score,
                acceptance_margin=self.acceptance_margin,
                score_scale_arcsec=self.score_scale_arcsec,
            )
            scored = match.candidates
            run.candidate_count = len(scored)
            selected_index = match.selected_index

            raw_rows: list[RawCatalogRow] = []
            detections: list[CatalogDetection] = []
            normalized_counts: list[int] = []
            for index, scored_candidate in enumerate(scored):
                candidate = scored_candidate.candidate
                ingested = DetectionIngestor.ingest(
                    session,
                    adapter=adapter,
                    candidate=candidate,
                    run_id=run.id,
                    separation_arcsec=scored_candidate.separation_arcsec,
                    score=scored_candidate.score,
                    accepted=index == selected_index,
                    target_id=target.id,
                    strict=index == selected_index,
                )
                raw_rows.append(ingested.raw_row)
                detections.append(ingested.detection)
                normalized_counts.append(
                    ingested.normalization.measurement_count
                )

            measurement_count = 0
            if not scored:
                run.status = ProviderRunStatus.NO_MATCH
            elif selected_index is None:
                run.status = ProviderRunStatus.AMBIGUOUS
            else:
                run.status = ProviderRunStatus.MATCH
                selected = scored[selected_index].candidate
                run.selected_source_id = selected.source_id
                measurement_count = normalized_counts[selected_index]
                shared_targets = shared_detection_target_ids(
                    session, target.id, detections[selected_index].id
                )
                store_catalog_attributes(
                    session,
                    selected,
                    run_id=run.id,
                    target_id=target.id,
                    raw_row_id=raw_rows[selected_index].id,
                    provider=adapter.name,
                )

            session.execute(
                update(CatalogRun)
                .where(
                    CatalogRun.target_id == target.id,
                    CatalogRun.provider == adapter.name,
                    CatalogRun.is_current.is_(True),
                    CatalogRun.id != run.id,
                )
                .values(is_current=False)
            )
            run.is_current = True
            run.completed_at = datetime.now(timezone.utc)
            if adapter.name in {"iras_psc", "iras_fsc"}:
                from .iras import reconcile_iras_target
                reconcile_iras_target(session, target.id)
            if previous_signature != catalog_run_signature(session, run):
                mark_export_dirty(
                    session,
                    target.id,
                    source_type="catalog",
                    source_id=run.id,
                    reason=f"{adapter.name} catalog result changed",
                )
            for shared_target_id in shared_targets if selected_index is not None else ():
                mark_export_dirty(
                    session,
                    shared_target_id,
                    source_type="catalog",
                    source_id=run.id,
                    reason=f"{adapter.name} source became shared",
                )
            session.commit()
            return CatalogRefreshResult(
                run.id,
                target.id,
                provider,
                ProviderRunStatus.parse(run.status, "catalog status"),
                len(scored),
                measurement_count,
                run.selected_source_id,
            )

    def refresh_many(
        self,
        target_references: Iterable[str | int],
        provider: str,
        *,
        chunk_size: int = 500,
    ) -> tuple[CatalogRefreshResult, ...]:
        if provider not in self.adapters:
            raise KeyError(f"unknown catalog provider: {provider}")
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        adapter = self.adapters[provider]
        references = tuple(dict.fromkeys(target_references))
        if not hasattr(adapter, "query_many"):
            return tuple(self.refresh(reference, provider) for reference in references)
        contexts = self._contexts(references, adapter)
        results = []
        for offset in range(0, len(contexts), chunk_size):
            chunk = contexts[offset:offset + chunk_size]
            with self.sessions.begin() as session:
                request = CatalogBatchRequest(
                    provider=adapter.name,
                    release=adapter.release,
                    target_count=len(chunk),
                    chunk_size=chunk_size,
                    status="running",
                )
                session.add(request)
                session.flush()
                request_id = request.id
            try:
                candidates_by_target = adapter.query_many(chunk)
            except ProviderError as error:
                with self.sessions.begin() as session:
                    request = session.get(CatalogBatchRequest, request_id)
                    request.status = "fallback"
                    request.error = str(error)
                    request.completed_at = datetime.now(timezone.utc)
                results.extend(
                    self.refresh(
                        context.target_id, provider, batch_request_id=request_id,
                    )
                    for context in chunk
                )
                continue
            with self.sessions.begin() as session:
                request = session.get(CatalogBatchRequest, request_id)
                request.status = "completed"
                request.completed_at = datetime.now(timezone.utc)
            for context in chunk:
                candidates = candidates_by_target.get(context.target_id)
                if candidates is None:
                    results.append(self.refresh(
                        context.target_id, provider, batch_request_id=request_id,
                    ))
                else:
                    results.append(self.refresh(
                        context.target_id,
                        provider,
                        preloaded_candidates=list(candidates),
                        batch_request_id=request_id,
                    ))
        return tuple(results)

    def _contexts(self, references, adapter) -> tuple[CatalogQueryContext, ...]:
        """Build query contexts in bulk when refresh_many receives target IDs."""
        if not all(isinstance(reference, int) or str(reference).isdigit() for reference in references):
            return tuple(self._context(reference, adapter) for reference in references)
        target_ids = [int(reference) for reference in references]
        targets_by_id: dict[int, Target] = {}
        astrometry_by_target: dict[int, Astrometry] = {}
        identifiers_by_target: dict[int, list[str]] = {
            target_id: [] for target_id in target_ids
        }
        with self.sessions() as session:
            for offset in range(0, len(target_ids), 500):
                chunk = target_ids[offset:offset + 500]
                for target in session.scalars(select(Target).where(Target.id.in_(chunk))):
                    targets_by_id[target.id] = target
            astrometry_by_target = best_target_astrometry_map(
                session, targets_by_id.values()
            )
            for offset in range(0, len(target_ids), 500):
                chunk = target_ids[offset:offset + 500]
                for target_id, value in session.execute(
                    select(ExternalIdentifier.target_id, ExternalIdentifier.value)
                    .where(ExternalIdentifier.target_id.in_(chunk))
                    .order_by(ExternalIdentifier.target_id, ExternalIdentifier.id)
                ):
                    identifiers_by_target[target_id].append(value)

        contexts = []
        for target_id in target_ids:
            target = targets_by_id.get(target_id)
            if target is None:
                raise KeyError(f"target not found: {target_id}")
            if target.canonical_astrometry_id is None:
                raise RuntimeError(f"target {target.sdbid} has no canonical astrometry")
            native = astrometry_by_target[target.id]
            contexts.append(CatalogQueryContext(
                target.id,
                target.sdbid,
                propagate_to_epoch(native, adapter.query_epoch),
                tuple(identifiers_by_target[target.id]),
            ))
        return tuple(contexts)

    def _context(self, target_reference, adapter) -> CatalogQueryContext:
        with self.sessions() as session:
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            solution = session.get(AstrometricSolution, target.canonical_astrometry_id)
            if solution is None:
                raise RuntimeError(f"target {target.sdbid} has no canonical astrometry")
            native = best_target_astrometry(session, target)
            identifiers = tuple(session.scalars(
                select(ExternalIdentifier.value)
                .where(ExternalIdentifier.target_id == target.id)
                .order_by(ExternalIdentifier.id)
            ))
            return CatalogQueryContext(
                target.id,
                target.sdbid,
                propagate_to_epoch(native, adapter.query_epoch),
                identifiers,
            )
