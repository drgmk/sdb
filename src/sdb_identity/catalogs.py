from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import angular_separation_arcsec, propagate_to_epoch
from .catalog_resolution import default_resolution
from .catalog_provenance import CatalogProvenance, provenance_from_payload
from .catalog_results import effective_catalog_results
from .decisions import DecisionContext
from .dirty import mark_export_dirty
from .models import (
    AstrometricSolution,
    CatalogBatchRequest,
    CatalogDetection,
    CatalogDetectionProvenance,
    CatalogRun,
    CatalogAttribute,
    CatalogResultDecision,
    CatalogRetryAction,
    ExternalIdentifier,
    NormalizedMeasurement,
    RawCatalogRow,
    Target,
)
from .photometry_semantics import validate_photometry_semantics
from .providers import Astrometry, ProviderError
from .targets import resolve_target
from .vocabulary import PROVIDER_FAILURE_STATUSES, ProviderRunStatus


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeasurementValue:
    band: str
    value: float
    error: float = 0.0
    systematic_error: float = 0.0
    unit: str = "mag"
    bibcode: str = ""
    quality: str | None = None
    upper_limit: bool = False
    excluded: bool = False
    exclusion_reason: str | None = None
    note1: str = ""
    note2: str = ""
    private: bool = False
    resolution_major_arcsec: float | None = None
    resolution_minor_arcsec: float | None = None
    resolution_kind: str | None = None
    resolution_reference: str | None = None
    ownership_scope: str = "component"
    blend_state: str = "clear"
    blend_reason: str | None = None
    measurement_key: str | None = None


@dataclass(frozen=True)
class CatalogAttributeValue:
    key: str
    value_text: str | None = None
    value_float: float | None = None
    uncertainty: float | None = None
    unit: str | None = None
    quality: str | None = None
    reference: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class CatalogCandidate:
    source_id: str
    ra_deg: float
    dec_deg: float
    epoch: float
    payload: Mapping[str, object]
    measurements: tuple[MeasurementValue, ...] = field(default_factory=tuple)
    attributes: tuple[CatalogAttributeValue, ...] = field(default_factory=tuple)
    detection_key: str | None = None
    provenance: tuple[CatalogProvenance, ...] = field(default_factory=tuple)

    @property
    def astrometry(self) -> Astrometry:
        return Astrometry(self.ra_deg, self.dec_deg, self.epoch, source="catalog", source_id=self.source_id)


@dataclass(frozen=True)
class CatalogQueryContext:
    target_id: int
    sdbid: str
    astrometry: Astrometry
    identifiers: tuple[str, ...] = ()


class CatalogAdapter(Protocol):
    name: str
    release: str
    query_epoch: float

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]: ...

    def normalize(self, candidate: CatalogCandidate) -> tuple[MeasurementValue, ...]: ...


class BulkCatalogAdapter(CatalogAdapter, Protocol):
    def query_many(
        self, contexts: tuple[CatalogQueryContext, ...]
    ) -> Mapping[int, list[CatalogCandidate]]: ...


@dataclass(frozen=True)
class CatalogRefreshResult:
    run_id: int
    target_id: int
    provider: str
    status: ProviderRunStatus
    candidate_count: int
    measurement_count: int
    selected_source_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class DetectionNormalizationItem:
    detection_id: int
    provider: str
    source_id: str
    status: str
    measurement_count: int
    error: str | None = None


@dataclass(frozen=True)
class DetectionNormalizationSummary:
    detection_count: int
    completed: int
    no_measurements: int
    failed: int
    measurement_count: int
    items: tuple[DetectionNormalizationItem, ...]


class CatalogService:
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
            native = Astrometry(
                ra_deg=solution.ra_deg,
                dec_deg=solution.dec_deg,
                epoch=solution.epoch,
                pm_ra_cosdec_masyr=solution.pm_ra_cosdec_masyr,
                pm_dec_masyr=solution.pm_dec_masyr,
                parallax_mas=solution.parallax_mas,
                radial_velocity_kms=solution.radial_velocity_kms,
                source=solution.source,
                source_id=solution.source_id,
                position_bibcode=solution.position_bibcode,
                proper_motion_bibcode=solution.proper_motion_bibcode,
                parallax_bibcode=solution.parallax_bibcode,
                radial_velocity_bibcode=solution.radial_velocity_bibcode,
            )
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
            previous_signature = self._run_signature(
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

            scored = []
            for candidate in candidates:
                if hasattr(adapter, "candidate_separation"):
                    separation = adapter.candidate_separation(context, candidate)
                else:
                    separation = angular_separation_arcsec(
                        query_astrometry,
                        candidate.astrometry,
                        epoch=adapter.query_epoch,
                    )
                if hasattr(adapter, "score_candidate"):
                    score = adapter.score_candidate(context, candidate, separation)
                else:
                    score = math.exp(-0.5 * (separation / self.score_scale_arcsec) ** 2)
                scored.append((candidate, separation, score))
            scored.sort(key=lambda item: item[2], reverse=True)
            run.candidate_count = len(scored)

            selected_index = None
            if scored:
                best = scored[0][2]
                runner_up = scored[1][2] if len(scored) > 1 else 0.0
                if best >= self.acceptance_score and best - runner_up >= self.acceptance_margin:
                    selected_index = 0

            raw_rows: list[RawCatalogRow] = []
            detections: list[CatalogDetection] = []
            normalized_counts: list[int] = []
            for index, (candidate, separation, score) in enumerate(scored):
                detection = self._canonical_detection(session, adapter, candidate)
                row = RawCatalogRow(
                    run_id=run.id,
                    detection_id=detection.id,
                    source_id=candidate.source_id,
                    ra_deg=candidate.ra_deg,
                    dec_deg=candidate.dec_deg,
                    epoch=candidate.epoch,
                    separation_arcsec=separation,
                    score=score,
                    accepted=index == selected_index,
                    payload_json=json.dumps(candidate.payload, sort_keys=True, ensure_ascii=False),
                )
                session.add(row)
                session.flush()
                raw_rows.append(row)
                detections.append(detection)
                item = self._normalize_detection(
                    session,
                    adapter=adapter,
                    candidate=candidate,
                    detection=detection,
                    run_id=run.id,
                    target_id=target.id,
                    raw_row_id=row.id,
                    strict=index == selected_index,
                )
                normalized_counts.append(item.measurement_count)

            measurement_count = 0
            if not scored:
                run.status = ProviderRunStatus.NO_MATCH
            elif selected_index is None:
                run.status = ProviderRunStatus.AMBIGUOUS
            else:
                run.status = ProviderRunStatus.MATCH
                selected = scored[selected_index][0]
                run.selected_source_id = selected.source_id
                measurement_count = normalized_counts[selected_index]
                shared_targets = self._mark_shared_detection(
                    session, run.id, target.id, detections[selected_index].id
                )
                self._store_attributes(
                    session,
                    selected,
                    run.id,
                    target.id,
                    raw_rows[selected_index].id,
                    adapter.name,
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
            if previous_signature != self._run_signature(session, run):
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
        solutions_by_id: dict[int, AstrometricSolution] = {}
        identifiers_by_target: dict[int, list[str]] = {
            target_id: [] for target_id in target_ids
        }
        with self.sessions() as session:
            for offset in range(0, len(target_ids), 500):
                chunk = target_ids[offset:offset + 500]
                for target in session.scalars(select(Target).where(Target.id.in_(chunk))):
                    targets_by_id[target.id] = target
            solution_ids = [
                target.canonical_astrometry_id
                for target in targets_by_id.values()
                if target.canonical_astrometry_id is not None
            ]
            for offset in range(0, len(solution_ids), 500):
                chunk = solution_ids[offset:offset + 500]
                for solution in session.scalars(
                    select(AstrometricSolution).where(AstrometricSolution.id.in_(chunk))
                ):
                    solutions_by_id[solution.id] = solution
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
            solution = solutions_by_id.get(target.canonical_astrometry_id)
            if solution is None:
                raise RuntimeError(f"target {target.sdbid} has no canonical astrometry")
            native = Astrometry(
                ra_deg=solution.ra_deg,
                dec_deg=solution.dec_deg,
                epoch=solution.epoch,
                pm_ra_cosdec_masyr=solution.pm_ra_cosdec_masyr,
                pm_dec_masyr=solution.pm_dec_masyr,
                parallax_mas=solution.parallax_mas,
                radial_velocity_kms=solution.radial_velocity_kms,
                source=solution.source,
                source_id=solution.source_id,
                position_bibcode=solution.position_bibcode,
                proper_motion_bibcode=solution.proper_motion_bibcode,
                parallax_bibcode=solution.parallax_bibcode,
                radial_velocity_bibcode=solution.radial_velocity_bibcode,
            )
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
            native = Astrometry(
                ra_deg=solution.ra_deg,
                dec_deg=solution.dec_deg,
                epoch=solution.epoch,
                pm_ra_cosdec_masyr=solution.pm_ra_cosdec_masyr,
                pm_dec_masyr=solution.pm_dec_masyr,
                parallax_mas=solution.parallax_mas,
                radial_velocity_kms=solution.radial_velocity_kms,
                source=solution.source,
                source_id=solution.source_id,
                position_bibcode=solution.position_bibcode,
                proper_motion_bibcode=solution.proper_motion_bibcode,
                parallax_bibcode=solution.parallax_bibcode,
                radial_velocity_bibcode=solution.radial_velocity_bibcode,
            )
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

    @staticmethod
    def _canonical_detection(
        session: Session,
        adapter: CatalogAdapter,
        candidate: CatalogCandidate,
    ) -> CatalogDetection:
        detection_key_factory = getattr(adapter, "detection_key", None)
        if callable(detection_key_factory):
            detection_key = str(detection_key_factory(candidate))
        else:
            detection_key = candidate.detection_key or candidate.source_id
        detection_key = detection_key.strip()
        if not detection_key:
            raise ValueError(f"{adapter.name} candidate has no stable detection key")
        payload_json = json.dumps(candidate.payload, sort_keys=True, ensure_ascii=False)
        session.execute(
            sqlite_insert(CatalogDetection)
            .values(
                provider=adapter.name,
                release=adapter.release,
                detection_key=detection_key,
                source_id=candidate.source_id,
                ra_deg=candidate.ra_deg,
                dec_deg=candidate.dec_deg,
                epoch=candidate.epoch,
                payload_json=payload_json,
                normalization_status="pending",
                normalization_error=None,
                normalized_at=None,
                created_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=["provider", "release", "detection_key"],
                set_={
                    "source_id": candidate.source_id,
                    "ra_deg": candidate.ra_deg,
                    "dec_deg": candidate.dec_deg,
                    "epoch": candidate.epoch,
                    "payload_json": payload_json,
                    "normalization_status": "pending",
                    "normalization_error": None,
                    "normalized_at": None,
                },
            )
        )
        detection = session.scalar(select(CatalogDetection).where(
            CatalogDetection.provider == adapter.name,
            CatalogDetection.release == adapter.release,
            CatalogDetection.detection_key == detection_key,
        ))
        if detection is None:
            raise RuntimeError("failed to create or retrieve canonical catalog detection")
        CatalogService._store_detection_provenance(
            session, detection, candidate
        )
        return detection

    @staticmethod
    def _store_detection_provenance(
        session: Session,
        detection: CatalogDetection,
        candidate: CatalogCandidate,
    ) -> None:
        provenance = (
            candidate.provenance
            or provenance_from_payload(candidate.payload)
            or (CatalogProvenance(
                service=detection.provider,
                catalog_id=detection.release.split("@", 1)[0],
            ),)
        )
        for item in provenance:
            session.execute(
                sqlite_insert(CatalogDetectionProvenance)
                .values(
                    detection_id=detection.id,
                    provenance_key=item.key,
                    role=item.role,
                    service=item.service,
                    catalog_id=item.catalog_id,
                    table_id=item.table_id,
                    row_key=item.row_key,
                    identifier_column=item.identifier_column,
                    identifier_value=item.identifier_value,
                    source_url=item.source_url,
                    access_url=item.access_url,
                    readme_url=item.readme_url,
                    created_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_nothing(
                    index_elements=["detection_id", "provenance_key"]
                )
            )

    def normalize_detections(
        self,
        detection_ids: Iterable[int],
    ) -> DetectionNormalizationSummary:
        """Normalize stored provider payloads without repeating provider queries."""
        ids = tuple(dict.fromkeys(int(value) for value in detection_ids))
        if not ids:
            return self._normalization_summary([])
        items = []
        with self.sessions() as session, session.begin():
            detections = list(session.scalars(
                select(CatalogDetection)
                .where(CatalogDetection.id.in_(ids))
                .order_by(CatalogDetection.id)
            ))
            for detection in detections:
                adapter = self.adapters.get(detection.provider)
                if adapter is None:
                    items.append(DetectionNormalizationItem(
                        detection.id,
                        detection.provider,
                        detection.source_id,
                        "failed",
                        0,
                        f"catalog adapter is unavailable: {detection.provider}",
                    ))
                    continue
                encounter = session.execute(
                    select(RawCatalogRow, CatalogRun)
                    .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
                    .where(RawCatalogRow.detection_id == detection.id)
                    .order_by(RawCatalogRow.id)
                    .limit(1)
                ).first()
                if encounter is None:
                    detection.normalization_status = "failed"
                    detection.normalization_error = "detection has no query encounter"
                    detection.normalized_at = datetime.now(timezone.utc)
                    items.append(DetectionNormalizationItem(
                        detection.id,
                        detection.provider,
                        detection.source_id,
                        "failed",
                        0,
                        detection.normalization_error,
                    ))
                    continue
                raw, run = encounter
                try:
                    candidate = self._candidate_from_payload(
                        adapter, json.loads(detection.payload_json)
                    )
                    self._store_detection_provenance(
                        session, detection, candidate
                    )
                except Exception as error:
                    detection.normalization_status = "failed"
                    detection.normalization_error = str(error)
                    detection.normalized_at = datetime.now(timezone.utc)
                    items.append(DetectionNormalizationItem(
                        detection.id,
                        detection.provider,
                        detection.source_id,
                        "failed",
                        0,
                        str(error),
                    ))
                    continue
                items.append(self._normalize_detection(
                    session,
                    adapter=adapter,
                    candidate=candidate,
                    detection=detection,
                    run_id=run.id,
                    target_id=run.target_id,
                    raw_row_id=raw.id,
                    strict=False,
                ))
        return self._normalization_summary(items)

    @staticmethod
    def _candidate_from_payload(
        adapter: CatalogAdapter,
        payload: dict[str, object],
    ) -> CatalogCandidate:
        if hasattr(adapter, "candidate_from_payload"):
            candidate = adapter.candidate_from_payload(payload)
        elif hasattr(adapter, "parse_row"):
            candidate = adapter.parse_row(payload)
        else:
            raise ValueError(
                f"{adapter.name} adapter cannot reconstruct stored candidates"
            )
        if candidate.provenance:
            return candidate
        return CatalogCandidate(
            source_id=candidate.source_id,
            ra_deg=candidate.ra_deg,
            dec_deg=candidate.dec_deg,
            epoch=candidate.epoch,
            payload=candidate.payload,
            measurements=candidate.measurements,
            attributes=candidate.attributes,
            detection_key=candidate.detection_key,
            provenance=provenance_from_payload(payload),
        )

    def _normalize_detection(
        self,
        session: Session,
        *,
        adapter: CatalogAdapter,
        candidate: CatalogCandidate,
        detection: CatalogDetection,
        run_id: int,
        target_id: int,
        raw_row_id: int,
        strict: bool,
    ) -> DetectionNormalizationItem:
        try:
            values = adapter.normalize(candidate)
            count = 0
            for value_index, value in enumerate(values):
                self._canonical_measurement(
                    session,
                    adapter=adapter,
                    candidate=candidate,
                    detection=detection,
                    value=value,
                    value_index=value_index,
                    run_id=run_id,
                    target_id=target_id,
                    raw_row_id=raw_row_id,
                )
                count += 1
        except Exception as error:
            if strict:
                raise
            LOGGER.warning(
                "could not normalize unaccepted %s detection %s",
                adapter.name,
                candidate.source_id,
                exc_info=True,
            )
            detection.normalization_status = "failed"
            detection.normalization_error = str(error)
            detection.normalized_at = datetime.now(timezone.utc)
            return DetectionNormalizationItem(
                detection.id,
                detection.provider,
                detection.source_id,
                "failed",
                0,
                str(error),
            )
        detection.normalization_status = (
            "completed" if count else "no_measurements"
        )
        detection.normalization_error = None
        detection.normalized_at = datetime.now(timezone.utc)
        return DetectionNormalizationItem(
            detection.id,
            detection.provider,
            detection.source_id,
            detection.normalization_status,
            count,
        )

    @staticmethod
    def _normalization_summary(
        items: list[DetectionNormalizationItem],
    ) -> DetectionNormalizationSummary:
        return DetectionNormalizationSummary(
            detection_count=len(items),
            completed=sum(item.status == "completed" for item in items),
            no_measurements=sum(
                item.status == "no_measurements" for item in items
            ),
            failed=sum(item.status == "failed" for item in items),
            measurement_count=sum(item.measurement_count for item in items),
            items=tuple(items),
        )

    @staticmethod
    def _canonical_measurement(
        session: Session,
        *,
        adapter: CatalogAdapter,
        candidate: CatalogCandidate,
        detection: CatalogDetection,
        value: MeasurementValue,
        value_index: int,
        run_id: int,
        target_id: int,
        raw_row_id: int,
    ) -> NormalizedMeasurement:
        ownership_scope, blend_state = validate_photometry_semantics(
            value.ownership_scope, value.blend_state
        )
        measurement_key_factory = getattr(adapter, "measurement_key", None)
        if callable(measurement_key_factory):
            measurement_key = str(measurement_key_factory(candidate, value, value_index))
        else:
            measurement_key = value.measurement_key or value.band
        measurement_key = measurement_key.strip()
        if not measurement_key:
            raise ValueError(f"{adapter.name} measurement has no stable key")
        resolution = default_resolution(adapter.name, value.band)
        values = {
            "run_id": run_id,
            "target_id": target_id,
            "raw_row_id": raw_row_id,
            "detection_id": detection.id,
            "measurement_key": measurement_key,
            "provider": adapter.name,
            "source_id": candidate.source_id,
            "band": value.band,
            "value": value.value,
            "error": value.error,
            "systematic_error": value.systematic_error,
            "upper_limit": value.upper_limit,
            "unit": value.unit,
            "bibcode": value.bibcode,
            "quality": value.quality,
            "note1": value.note1,
            "note2": value.note2,
            "private": value.private,
            "excluded": value.excluded,
            "exclusion_reason": value.exclusion_reason,
            "resolution_major_arcsec": (
                value.resolution_major_arcsec
                if value.resolution_major_arcsec is not None
                else None if resolution is None else resolution.major_arcsec
            ),
            "resolution_minor_arcsec": (
                value.resolution_minor_arcsec
                if value.resolution_minor_arcsec is not None
                else None if resolution is None else resolution.minor_arcsec
            ),
            "resolution_kind": value.resolution_kind or (
                None if resolution is None else resolution.kind
            ),
            "resolution_reference": value.resolution_reference or (
                None if resolution is None else resolution.reference
            ),
            "ownership_scope": ownership_scope,
            "blend_state": blend_state,
            "blend_reason": value.blend_reason,
        }
        # Provider-native values may be corrected without a release-label
        # change. Refresh those fields in place so every target encounter sees
        # the same current representation, while preserving SDB curation
        # fields (exclusion and assignment state) and first-seen
        # provenance on the canonical row.
        provider_values = {
            key: values[key]
            for key in (
                "provider", "source_id", "band", "value", "error",
                "systematic_error", "upper_limit", "unit", "bibcode",
                "quality", "note1", "note2", "private",
                "resolution_major_arcsec", "resolution_minor_arcsec",
                "resolution_kind", "resolution_reference",
                "ownership_scope", "blend_state", "blend_reason",
            )
        }
        session.execute(
            sqlite_insert(NormalizedMeasurement)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["detection_id", "measurement_key"],
                set_=provider_values,
            )
        )
        measurement = session.scalar(select(NormalizedMeasurement).where(
            NormalizedMeasurement.detection_id == detection.id,
            NormalizedMeasurement.measurement_key == measurement_key,
        ))
        if measurement is None:
            raise RuntimeError("failed to create or retrieve canonical measurement")
        return measurement

    def override_candidate(
        self,
        raw_row_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> CatalogRefreshResult:
        with self.sessions() as session, session.begin():
            selected_raw = session.get(RawCatalogRow, raw_row_id)
            if selected_raw is None:
                raise KeyError(f"catalog candidate not found: {raw_row_id}")
            previous = session.get(CatalogRun, selected_raw.run_id)
            if previous is None or not previous.is_current:
                raise ValueError("catalog candidate is not from the current run")
            latest_decision = session.scalar(
                select(CatalogResultDecision)
                .where(CatalogResultDecision.reviewed_run_id == previous.id)
                .order_by(CatalogResultDecision.id.desc())
                .limit(1)
            )
            if (
                latest_decision is not None
                and latest_decision.action == "accept_detection"
                and latest_decision.reviewed_raw_row_id == selected_raw.id
            ):
                measurement_count = session.query(
                    NormalizedMeasurement
                ).where(
                    NormalizedMeasurement.detection_id
                    == selected_raw.detection_id
                ).count()
                return CatalogRefreshResult(
                    previous.id,
                    previous.target_id,
                    previous.provider,
                    ProviderRunStatus.MATCH,
                    previous.candidate_count,
                    measurement_count,
                    selected_raw.source_id,
                )
            adapter = self.adapters.get(previous.provider)
            if adapter is None:
                raise KeyError(f"catalog adapter is unavailable: {previous.provider}")
            payload = json.loads(selected_raw.payload_json)
            if hasattr(adapter, "candidate_from_payload"):
                candidate = adapter.candidate_from_payload(payload)
            elif hasattr(adapter, "parse_row"):
                candidate = adapter.parse_row(payload)
            else:
                raise ValueError(f"catalog adapter cannot reconstruct candidates: {previous.provider}")
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Selected {previous.provider} source {candidate.source_id} "
                    f"for target {previous.target_id}"
                ),
            )

            detection = session.get(CatalogDetection, selected_raw.detection_id)
            measurement_count = session.query(NormalizedMeasurement).where(
                NormalizedMeasurement.detection_id == detection.id
            ).count()
            self._store_attributes(
                session,
                candidate,
                previous.id,
                previous.target_id,
                selected_raw.id,
                previous.provider,
            )
            override = CatalogResultDecision(
                target_id=previous.target_id,
                provider=previous.provider,
                reviewed_run_id=previous.id,
                action="accept_detection",
                accepted_detection_id=detection.id,
                reviewed_raw_row_id=selected_raw.id,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(override)
            session.flush()
            if previous.provider in {"iras_psc", "iras_fsc"}:
                from .iras import reconcile_iras_target
                reconcile_iras_target(session, previous.target_id)
            mark_export_dirty(
                session,
                previous.target_id,
                source_type="catalog_result_decision",
                source_id=override.id,
                reason="manual catalog candidate selection",
            )
            return CatalogRefreshResult(
                previous.id,
                previous.target_id,
                previous.provider,
                ProviderRunStatus.MATCH,
                previous.candidate_count,
                measurement_count,
                candidate.source_id,
            )

    def override_no_match(
        self,
        run_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> CatalogRefreshResult:
        """Record an audited no-match interpretation of an ambiguous run."""
        with self.sessions() as session, session.begin():
            previous = session.get(CatalogRun, run_id)
            if previous is None:
                raise KeyError(f"catalog run not found: {run_id}")
            if not previous.is_current:
                raise ValueError("catalog run is no longer current")
            if previous.status != ProviderRunStatus.AMBIGUOUS:
                raise ValueError("reviewed no-match requires a current ambiguous result")
            latest_decision = session.scalar(
                select(CatalogResultDecision)
                .where(CatalogResultDecision.reviewed_run_id == previous.id)
                .order_by(CatalogResultDecision.id.desc())
                .limit(1)
            )
            if (
                latest_decision is not None
                and latest_decision.action == "reviewed_no_match"
            ):
                return CatalogRefreshResult(
                    previous.id,
                    previous.target_id,
                    previous.provider,
                    ProviderRunStatus.NO_MATCH,
                    previous.candidate_count,
                    0,
                    None,
                )
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Reviewed {previous.provider} candidates for target "
                    f"{previous.target_id}; none is the target"
                ),
            )
            action = CatalogResultDecision(
                target_id=previous.target_id,
                provider=previous.provider,
                reviewed_run_id=previous.id,
                action="reviewed_no_match",
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(action)
            session.flush()
            if previous.provider in {"iras_psc", "iras_fsc"}:
                from .iras import reconcile_iras_target
                reconcile_iras_target(session, previous.target_id)
            mark_export_dirty(
                session,
                previous.target_id,
                source_type="catalog_result_decision",
                source_id=action.id,
                reason="reviewed catalog no-match",
            )
            return CatalogRefreshResult(
                previous.id,
                previous.target_id,
                previous.provider,
                ProviderRunStatus.NO_MATCH,
                previous.candidate_count,
                0,
            )

    def retry_failed_run(
        self,
        run_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> CatalogRefreshResult:
        """Retry a failed provider attempt and audit the operator request."""
        with self.sessions() as session:
            previous = session.get(CatalogRun, run_id)
            if previous is None:
                raise KeyError(f"catalog run not found: {run_id}")
            if previous.status not in PROVIDER_FAILURE_STATUSES:
                raise ValueError("retry requires a failed catalog run")
            latest_id = session.scalar(
                select(CatalogRun.id)
                .where(
                    CatalogRun.target_id == previous.target_id,
                    CatalogRun.provider == previous.provider,
                )
                .order_by(CatalogRun.id.desc())
                .limit(1)
            )
            if latest_id != previous.id:
                raise ValueError("catalog failure has already been superseded")
            target_id = previous.target_id
            provider = previous.provider
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Retried failed {provider} provider result for target {target_id}"
                ),
            )

        result = self.refresh(target_id, provider)
        with self.sessions() as session, session.begin():
            action = CatalogRetryAction(
                target_id=target_id,
                provider=provider,
                failed_run_id=run_id,
                retry_run_id=result.run_id,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(action)
        return result

    @staticmethod
    def _mark_shared_detection(
        session: Session,
        run_id: int,
        target_id: int,
        detection_id: int,
    ) -> set[int]:
        from .catalog_measurements import (
            current_catalog_detection_target_pairs,
        )

        target_ids = {
            current_target_id
            for current_detection_id, current_target_id
            in current_catalog_detection_target_pairs(session, [detection_id])
            if current_detection_id == detection_id
        }
        target_ids.add(target_id)
        affected = target_ids - {target_id}
        return affected

    @staticmethod
    def _run_signature(
        session: Session,
        run: CatalogRun | None,
        *,
        effective_status: str | ProviderRunStatus | None = None,
        selected_source_id: str | None = None,
        selected_raw_row_id: int | None = None,
    ):
        if run is None:
            return None
        rows = tuple(session.execute(select(
            RawCatalogRow.source_id,
            RawCatalogRow.accepted,
            RawCatalogRow.payload_json,
        ).where(RawCatalogRow.run_id == run.id).order_by(RawCatalogRow.id)).all())
        measurements = tuple(session.execute(select(
            NormalizedMeasurement.band,
            NormalizedMeasurement.value,
            NormalizedMeasurement.error,
            NormalizedMeasurement.upper_limit,
            NormalizedMeasurement.excluded,
            NormalizedMeasurement.quality,
            NormalizedMeasurement.blend_state,
            NormalizedMeasurement.ownership_scope,
        )
        .join(
            RawCatalogRow,
            RawCatalogRow.detection_id == NormalizedMeasurement.detection_id,
        )
        .where(
            RawCatalogRow.run_id == run.id,
            (
                RawCatalogRow.id == selected_raw_row_id
                if selected_raw_row_id is not None
                else RawCatalogRow.accepted.is_(True)
            ),
        )
        .order_by(NormalizedMeasurement.id)).all())
        attributes = tuple(session.execute(select(
            CatalogAttribute.key,
            CatalogAttribute.value_text,
            CatalogAttribute.value_float,
            CatalogAttribute.uncertainty,
            CatalogAttribute.unit,
            CatalogAttribute.quality,
        ).where(CatalogAttribute.run_id == run.id).order_by(CatalogAttribute.id)).all())
        return (
            run.status if effective_status is None else str(effective_status),
            (
                run.selected_source_id
                if selected_source_id is None
                else selected_source_id
            ),
            rows,
            measurements,
            attributes,
        )

    @staticmethod
    def _store_attributes(
        session: Session,
        candidate: CatalogCandidate,
        run_id: int,
        target_id: int,
        raw_row_id: int,
        provider: str,
    ) -> None:
        for value in candidate.attributes:
            if value.value_text is None and value.value_float is None:
                continue
            session.add(CatalogAttribute(
                run_id=run_id,
                target_id=target_id,
                raw_row_id=raw_row_id,
                provider=provider,
                source_id=candidate.source_id,
                key=value.key,
                value_text=value.value_text,
                value_float=value.value_float,
                uncertainty=value.uncertainty,
                unit=value.unit,
                quality=value.quality,
                reference=value.reference,
                note=value.note,
            ))
