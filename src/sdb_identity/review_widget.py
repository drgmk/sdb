from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .catalogs.policy import catalog_source_display_name
from .catalogs.adapters.review_metadata import normalize_review_payload
from .photometry.review import build_measurement_assignment_review
from .catalogs.results import (
    effective_catalog_results,
    effective_catalog_selected_rows,
)

from .astrometry import propagate_to_epoch
from .hierarchy.system_context import HierarchySystemContextService
from .hierarchy.geometry import hierarchy_record_positions
from .hierarchy.graph import (
    GRAPH_EDGE_STATUSES,
    edge_row,
    latest_overrides,
)
from .hierarchy.wds import UNUSABLE_SEPARATION_ARCSEC
from .identity_results import effective_identity_candidate_ids
from .models.identity import AstrometricSolution, MatchCandidate, Submission, Target
from .models.catalogs import (
    CatalogAttribute,
    CatalogDetectionProvenance,
    CatalogRun,
    NormalizedMeasurement,
    RawCatalogRow,
)
from .models.hierarchy import HierarchyMatchCandidate, HierarchyRecord, StructuralEdge
from .models.metadata import SimbadMetadata
from .providers import Astrometry
from .targets import resolve_target
from .catalogs.ubv_components import decode_ubv_component
from .catalogs.tdsc_components import decode_tdsc_component
from .catalogs.v70a_components import decode_v70a_component
from .vocabulary import PROVIDER_FAILURE_STATUSES


@dataclass(frozen=True)
class PhotometryBeam:
    provider: str
    band: str
    major_arcsec: float
    minor_arcsec: float | None = None
    kind: str | None = None
    reference: str | None = None
    ownership_scope: str = "component"
    blend_state: str = "clear"
    blend_reason: str | None = None
    value: float | None = None
    error: float | None = None
    unit: str | None = None
    upper_limit: bool = False


@dataclass(frozen=True)
class SkyPoint:
    kind: str
    provider: str
    status: str
    source_id: str
    ra_deg: float
    dec_deg: float
    separation_arcsec: float
    source_display_name: str = ""
    score: float | None = None
    accepted: bool = False
    run_id: int | None = None
    raw_row_id: int | None = None
    candidate_id: int | None = None
    detection_id: int | None = None
    target_id: int | None = None
    run_target_sdbid: str | None = None
    native_epoch: float | None = None
    native_ra_deg: float | None = None
    native_dec_deg: float | None = None
    display_epoch: float = 2000.0
    pm_ra_cosdec_masyr: float | None = None
    pm_dec_masyr: float | None = None
    pm_source: str | None = None
    photometry: tuple[str, ...] = ()
    photometry_beams: tuple[PhotometryBeam, ...] = ()
    attributes: tuple[str, ...] = ()
    linked_target_sdbids: tuple[str, ...] = ()
    cross_candidate_reason: str | None = None
    uncertainty_major_arcsec: float | None = None
    uncertainty_minor_arcsec: float | None = None
    catalog_component: str | None = None
    provenance: tuple[dict[str, object], ...] = ()
    note: str = ""


@dataclass(frozen=True)
class SkyArrow:
    kind: str
    provider: str
    source_id: str
    ra_deg: float
    dec_deg: float
    pm_ra_cosdec_masyr: float
    pm_dec_masyr: float
    years: float
    target_id: int | None = None
    note: str = ""


@dataclass(frozen=True)
class SkySegment:
    kind: str
    provider: str
    status: str
    source_id: str
    label: str
    start_ra_deg: float
    start_dec_deg: float
    end_ra_deg: float
    end_dec_deg: float
    candidate_id: int | None = None
    target_id: int | None = None
    native_id: str | None = None
    reference_label: str | None = None
    component_label: str | None = None
    relation_type: str = "component"
    structural_role: str = "non_structural"
    note: str = ""


@dataclass(frozen=True)
class ReviewSkyView:
    target_id: int
    sdbid: str
    center_ra_deg: float
    center_dec_deg: float
    radius_arcsec: float
    points: tuple[SkyPoint, ...]
    arrows: tuple[SkyArrow, ...] = ()
    segments: tuple[SkySegment, ...] = ()
    system_context: dict[str, object] | None = None


def build_review_sky_view(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    radius_arcsec: float | None = None,
) -> ReviewSkyView:
    if radius_arcsec is not None:
        radius_arcsec = float(radius_arcsec)
        if (
            not math.isfinite(radius_arcsec)
            or not 1.0 <= radius_arcsec <= 600.0
        ):
            raise ValueError("review radius must be between 1 and 600 arcsec")
    system_context = None
    with session_factory() as session:
        target = resolve_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        center = _target_center(session, target)
        solution = _target_solution(session, target)
        motion_solution = _target_motion_solution(session, target, solution)
        points = [
            SkyPoint(
                kind="target",
                provider="sdb",
                status="target",
                source_id=target.sdbid,
                ra_deg=center[0],
                dec_deg=center[1],
                separation_arcsec=0.0,
                accepted=True,
                target_id=target.id,
                pm_ra_cosdec_masyr=(
                    None
                    if motion_solution is None
                    else motion_solution.pm_ra_cosdec_masyr
                ),
                pm_dec_masyr=(
                    None if motion_solution is None else motion_solution.pm_dec_masyr
                ),
                pm_source=(
                    None if motion_solution is None else motion_solution.source
                ),
                note="canonical target position",
            )
        ]
        points.extend(
            _identity_points(
                session, target, center, motion_solution=motion_solution,
            )
        )
        points.extend(
            _catalog_points(
                session, target, center, motion_solution=motion_solution,
            )
        )
        points.extend(_simbad_metadata_points(session, target, center))
        hierarchy_points, hierarchy_segments = _hierarchy_points(session, target, center)
        points.extend(hierarchy_points)
        points = _deduplicate_points(points)
        arrows = []
        if motion_solution is not None:
            arrows.extend(_proper_motion_arrows(target, motion_solution))
        segments = list(hierarchy_segments)
        hierarchy_service = HierarchySystemContextService(session_factory)
        system_context = hierarchy_service.system_context(
            target.sdbid,
            radius_arcsec=radius_arcsec,
        )

        if radius_arcsec is None:
            farthest = max((point.separation_arcsec for point in points), default=1.0)
            farthest = max(
                farthest,
                max((_segment_farthest_offset(center, segment) for segment in segments), default=0.0),
            )
            explicit_member_sdbids = set(
                system_context.get("system_memberships_by_target") or {}
            )
            explicit_member_farthest = max(
                (
                    float(row["separation_arcsec"])
                    for row in system_context.get("nearby_sdb_targets") or []
                    if row.get("sdbid") in explicit_member_sdbids
                ),
                default=0.0,
            )
            farthest = max(farthest, explicit_member_farthest)
            radius_arcsec = min(600.0, max(60.0, math.ceil(farthest * 1.25)))
            if radius_arcsec != system_context["radius_arcsec"]:
                system_context = hierarchy_service.system_context(
                    target.sdbid,
                    radius_arcsec=radius_arcsec,
                )

        points = _annotate_catalog_target_candidates(
            session,
            target,
            center,
            points,
            system_context=system_context,
        )
        points = _deduplicate_points(points)
        assignment_review = build_measurement_assignment_review(
            session_factory,
            target.sdbid,
            system_context=system_context,
        )
        system_context["measurement_assignment_proposals"] = (
            assignment_review.proposals
        )
        system_context["measurement_assignment_matrix"] = assignment_review.matrix

        nearby_points, nearby_arrows = _nearby_target_points(
            session,
            target,
            center,
            radius_arcsec,
        )
        points.extend(nearby_points)
        points = _deduplicate_points(points)
        arrows.extend(nearby_arrows)
        points = _annotate_identity_cross_candidates(points, system_context)

    return ReviewSkyView(
        target_id=target.id,
        sdbid=target.sdbid,
        center_ra_deg=center[0],
        center_dec_deg=center[1],
        radius_arcsec=radius_arcsec,
        points=tuple(points),
        arrows=tuple(arrows),
        segments=tuple(segments),
        system_context=system_context,
    )


def _deduplicate_points(points: list[SkyPoint]) -> list[SkyPoint]:
    merged: dict[tuple[object, ...], SkyPoint] = {}
    order: list[tuple[object, ...]] = []
    for point in points:
        key = (
            point.provider,
            point.status,
            point.source_id,
            point.accepted,
            point.target_id,
            round(point.ra_deg, 9),
            round(point.dec_deg, 9),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = point
            order.append(key)
            continue
        merged[key] = _merge_duplicate_point(existing, point)
    return [merged[key] for key in order]


def _merge_duplicate_point(first: SkyPoint, second: SkyPoint) -> SkyPoint:
    kinds = []
    for kind in (*first.kind.split("+"), *second.kind.split("+")):
        if kind not in kinds:
            kinds.append(kind)
    notes = []
    for note in (first.note, second.note):
        if note and note not in notes:
            notes.append(note)
    photometry = tuple(dict.fromkeys((*first.photometry, *second.photometry)))
    photometry_beams = tuple(dict.fromkeys((*first.photometry_beams, *second.photometry_beams)))
    attributes = tuple(dict.fromkeys((*first.attributes, *second.attributes)))
    provenance = []
    for item in (*first.provenance, *second.provenance):
        if item not in provenance:
            provenance.append(item)
    return replace(
        first,
        kind="+".join(kinds),
        score=first.score if first.score is not None else second.score,
        run_id=first.run_id if first.run_id is not None else second.run_id,
        raw_row_id=first.raw_row_id if first.raw_row_id is not None else second.raw_row_id,
        candidate_id=first.candidate_id if first.candidate_id is not None else second.candidate_id,
        detection_id=first.detection_id if first.detection_id is not None else second.detection_id,
        target_id=first.target_id if first.target_id is not None else second.target_id,
        run_target_sdbid=first.run_target_sdbid or second.run_target_sdbid,
        native_epoch=first.native_epoch if first.native_epoch is not None else second.native_epoch,
        native_ra_deg=first.native_ra_deg if first.native_ra_deg is not None else second.native_ra_deg,
        native_dec_deg=first.native_dec_deg if first.native_dec_deg is not None else second.native_dec_deg,
        pm_ra_cosdec_masyr=first.pm_ra_cosdec_masyr if first.pm_ra_cosdec_masyr is not None else second.pm_ra_cosdec_masyr,
        pm_dec_masyr=first.pm_dec_masyr if first.pm_dec_masyr is not None else second.pm_dec_masyr,
        pm_source=first.pm_source if first.pm_source is not None else second.pm_source,
        photometry=photometry,
        photometry_beams=photometry_beams,
        attributes=attributes,
        linked_target_sdbids=tuple(dict.fromkeys((*first.linked_target_sdbids, *second.linked_target_sdbids))),
        cross_candidate_reason=first.cross_candidate_reason or second.cross_candidate_reason,
        uncertainty_major_arcsec=first.uncertainty_major_arcsec if first.uncertainty_major_arcsec is not None else second.uncertainty_major_arcsec,
        uncertainty_minor_arcsec=first.uncertainty_minor_arcsec if first.uncertainty_minor_arcsec is not None else second.uncertainty_minor_arcsec,
        catalog_component=first.catalog_component or second.catalog_component,
        provenance=tuple(provenance),
        note="; duplicate view row merged: ".join(notes),
    )


def _annotate_identity_cross_candidates(
    points: list[SkyPoint],
    system_context: dict[str, object] | None,
) -> list[SkyPoint]:
    if not system_context:
        return points
    cross_candidates = system_context.get("identity_cross_candidates") or []
    linked_by_candidate_id = {}
    linked_by_source = {}
    for row in cross_candidates:
        linked_targets = tuple(
            str(target["sdbid"])
            for target in row.get("matched_nearby_targets") or []
            if target.get("sdbid")
        )
        if not linked_targets:
            continue
        reason = (
            f"identity candidate resolves to nearby SDB target"
            f"{'s' if len(linked_targets) != 1 else ''}: {', '.join(linked_targets)}"
        )
        value = (linked_targets, reason)
        candidate_id = row.get("candidate_id")
        if candidate_id is not None:
            linked_by_candidate_id[int(candidate_id)] = value
        linked_by_source[(str(row.get("provider") or ""), str(row.get("source_id") or ""))] = value
    annotated = []
    for point in points:
        if point.kind != "identity":
            annotated.append(point)
            continue
        value = None
        if point.candidate_id is not None:
            value = linked_by_candidate_id.get(point.candidate_id)
        if value is None:
            value = linked_by_source.get((point.provider, point.source_id))
        if value is None:
            annotated.append(point)
            continue
        linked_targets, reason = value
        notes = [point.note, reason]
        annotated.append(replace(
            point,
            linked_target_sdbids=tuple(dict.fromkeys((*point.linked_target_sdbids, *linked_targets))),
            cross_candidate_reason=reason,
            note="; ".join(note for note in notes if note),
        ))
    return annotated


def _target_center(session: Session, target: Target) -> tuple[float, float]:
    solution = _target_solution(session, target)
    if solution is not None:
        return solution.derived_ra2000_deg, solution.derived_dec2000_deg
    return target.ra2000_deg, target.dec2000_deg


def _target_solution(session: Session, target: Target) -> AstrometricSolution | None:
    if target.canonical_astrometry_id is None:
        return None
    return session.get(AstrometricSolution, target.canonical_astrometry_id)


def _target_motion_solution(
    session: Session,
    target: Target,
    solution: AstrometricSolution | None = None,
) -> AstrometricSolution | Astrometry | None:
    solution = solution if solution is not None else _target_solution(session, target)
    if solution is not None and solution.proper_motion_available:
        return solution
    metadata = session.scalar(
        select(SimbadMetadata)
        .where(
            SimbadMetadata.target_id == target.id,
            SimbadMetadata.pm_ra_cosdec_masyr.is_not(None),
            SimbadMetadata.pm_dec_masyr.is_not(None),
        )
        .order_by(SimbadMetadata.id.desc())
        .limit(1)
    )
    if metadata is None:
        return solution
    return Astrometry(
        metadata.ra_deg,
        metadata.dec_deg,
        2000.0,
        pm_ra_cosdec_masyr=metadata.pm_ra_cosdec_masyr,
        pm_dec_masyr=metadata.pm_dec_masyr,
        source="simbad metadata",
        source_id=metadata.main_id,
        proper_motion_bibcode=metadata.proper_motion_bibcode,
    )


def _proper_motion_arrows(
    target: Target,
    solution: AstrometricSolution | Astrometry,
    *,
    years: float = 10.0,
) -> list[SkyArrow]:
    if (
        not solution.proper_motion_available
        or solution.pm_ra_cosdec_masyr is None
        or solution.pm_dec_masyr is None
    ):
        return []
    if isinstance(solution, AstrometricSolution):
        ra2000 = solution.derived_ra2000_deg
        dec2000 = solution.derived_dec2000_deg
    else:
        position = propagate_to_epoch(solution, 2000.0)
        ra2000 = position.ra_deg
        dec2000 = position.dec_deg
    return [
        SkyArrow(
            kind="proper_motion",
            provider=solution.source,
            source_id=solution.source_id or target.sdbid,
            ra_deg=ra2000,
            dec_deg=dec2000,
            pm_ra_cosdec_masyr=solution.pm_ra_cosdec_masyr,
            pm_dec_masyr=solution.pm_dec_masyr,
            years=years,
            target_id=target.id,
            note=f"{years:g} yr proper-motion vector",
        )
    ]


def _nearby_target_points(
    session: Session,
    target: Target,
    center: tuple[float, float],
    radius_arcsec: float,
) -> tuple[list[SkyPoint], list[SkyArrow]]:
    radius_deg = radius_arcsec / 3600.0
    dec0 = center[1]
    cos_dec = max(0.01, abs(math.cos(math.radians(dec0))))
    rows = session.scalars(
        select(Target)
        .where(Target.id != target.id)
        .where(Target.dec2000_deg.between(dec0 - radius_deg, dec0 + radius_deg))
        .where(Target.ra2000_deg.between(center[0] - radius_deg / cos_dec, center[0] + radius_deg / cos_dec))
        .order_by(Target.id)
    )
    points = []
    arrows = []
    for nearby in rows:
        separation = _separation_arcsec(center, nearby.ra2000_deg, nearby.dec2000_deg)
        if separation > radius_arcsec:
            continue
        solution = _target_solution(session, nearby)
        motion_solution = _target_motion_solution(session, nearby, solution)
        points.append(
            SkyPoint(
                kind="nearby_target",
                provider="sdb",
                status="nearby",
                source_id=nearby.sdbid,
                ra_deg=nearby.ra2000_deg,
                dec_deg=nearby.dec2000_deg,
                separation_arcsec=separation,
                target_id=nearby.id,
                pm_ra_cosdec_masyr=(
                    None
                    if motion_solution is None
                    else motion_solution.pm_ra_cosdec_masyr
                ),
                pm_dec_masyr=(
                    None if motion_solution is None else motion_solution.pm_dec_masyr
                ),
                pm_source=(
                    None if motion_solution is None else motion_solution.source
                ),
                note="nearby SDB target",
            )
        )
        if motion_solution is not None:
            arrows.extend(_proper_motion_arrows(nearby, motion_solution))
    return points, arrows


def _identity_points(
    session: Session,
    target: Target,
    center: tuple[float, float],
    *,
    motion_solution: AstrometricSolution | Astrometry | None = None,
) -> list[SkyPoint]:
    if motion_solution is None:
        motion_solution = _target_motion_solution(session, target)
    rows = session.execute(
        select(MatchCandidate, Submission)
        .join(Submission, Submission.id == MatchCandidate.submission_id)
        .where(Submission.target_id == target.id)
        .order_by(MatchCandidate.provider, MatchCandidate.score.desc(), MatchCandidate.id)
    )
    selected_ids = effective_identity_candidate_ids(
        session, target_ids=[target.id],
    )
    points = []
    for candidate, submission in rows:
        accepted = candidate.id in selected_ids
        status = "accepted" if accepted else "candidate"
        native_pm = None
        if (
            candidate.proper_motion_available
            and candidate.pm_ra_cosdec_masyr is not None
            and candidate.pm_dec_masyr is not None
        ):
            native_pm = (
                candidate.pm_ra_cosdec_masyr,
                candidate.pm_dec_masyr,
                candidate.provider,
            )
        pm_note = (
            ""
            if native_pm is not None
            else "; native candidate PM unavailable"
        )
        ra2000, dec2000, pm_ra, pm_dec, pm_source, note = _display_position_2000(
            candidate.ra_deg,
            candidate.dec_deg,
            candidate.epoch,
            motion_solution,
            native_pm=native_pm,
            base_note=f"identity candidate from submission {submission.id}{pm_note}",
        )
        points.append(
            SkyPoint(
                kind="identity",
                provider=candidate.provider,
                status=status,
                source_id=candidate.source_id,
                source_display_name=catalog_source_display_name(
                    candidate.provider, candidate.source_id
                ),
                ra_deg=ra2000,
                dec_deg=dec2000,
                separation_arcsec=_separation_arcsec(center, ra2000, dec2000),
                score=candidate.score,
                accepted=accepted,
                candidate_id=candidate.id,
                target_id=target.id,
                native_epoch=candidate.epoch,
                native_ra_deg=candidate.ra_deg,
                native_dec_deg=candidate.dec_deg,
                pm_ra_cosdec_masyr=pm_ra,
                pm_dec_masyr=pm_dec,
                pm_source=pm_source,
                note=note,
            )
        )
    return points


def _catalog_points(
    session: Session,
    target: Target,
    center: tuple[float, float],
    *,
    motion_solution: AstrometricSolution | Astrometry | None = None,
    run_statuses: set[str] | None = None,
) -> list[SkyPoint]:
    if motion_solution is None:
        motion_solution = _target_motion_solution(session, target)
    all_runs = list(session.scalars(
        select(CatalogRun)
        .where(CatalogRun.target_id == target.id)
        .order_by(CatalogRun.provider, CatalogRun.id)
    ))
    runs_by_provider: dict[str, list[CatalogRun]] = {}
    for run in all_runs:
        runs_by_provider.setdefault(run.provider, []).append(run)
    effective = effective_catalog_results(session, [target.id])
    runs: list[tuple[CatalogRun, str, frozenset[int]]] = []
    for provider_runs in runs_by_provider.values():
        current = next((run for run in provider_runs if run.is_current), None)
        latest = provider_runs[-1]
        current_result = (
            None
            if current is None
            else effective.get((target.id, current.provider))
        )
        current_status = (
            current.status
            if current_result is None
            else current_result.status.value
        ) if current is not None else None
        if current is not None and (
            run_statuses is None or current_status in run_statuses
        ):
            runs.append((
                current,
                current_status,
                frozenset(
                    raw.id
                    for raw, _detection in (
                        ()
                        if current_result is None
                        else effective_catalog_selected_rows(
                            session, current_result,
                        )
                    )
                ),
            ))
        if (
            latest.status in PROVIDER_FAILURE_STATUSES
            and (current is None or latest.id != current.id)
            and (run_statuses is None or latest.status in run_statuses)
        ):
            runs.append((latest, latest.status, frozenset()))
    runs.sort(key=lambda value: (value[0].provider, value[0].id))
    points = []
    for run, effective_status, selected_raw_row_ids in runs:
        rows = list(session.scalars(
            select(RawCatalogRow)
            .where(RawCatalogRow.run_id == run.id)
            .order_by(RawCatalogRow.score.desc(), RawCatalogRow.id)
        ))
        rows.sort(key=lambda row: row.id not in selected_raw_row_ids)
        if not rows:
            if run.status not in PROVIDER_FAILURE_STATUSES:
                continue
            ra2000, dec2000, pm_ra, pm_dec, pm_source, note = (
                _display_position_2000(
                    run.query_ra_deg,
                    run.query_dec_deg,
                    run.query_epoch,
                    motion_solution,
                    base_note=(
                        f"catalog run {run.id}; provider status {run.status}"
                        f"{f'; {run.error}' if run.error else ''}"
                    ),
                )
            )
            points.append(SkyPoint(
                kind="catalog",
                provider=run.provider,
                status=run.status,
                source_id="provider failure",
                ra_deg=ra2000,
                dec_deg=dec2000,
                separation_arcsec=_separation_arcsec(center, ra2000, dec2000),
                run_id=run.id,
                target_id=target.id,
                run_target_sdbid=target.sdbid,
                native_epoch=run.query_epoch,
                native_ra_deg=run.query_ra_deg,
                native_dec_deg=run.query_dec_deg,
                pm_ra_cosdec_masyr=pm_ra,
                pm_dec_masyr=pm_dec,
                pm_source=pm_source,
                note=note,
            ))
            continue
        for row in rows:
            payload = _catalog_payload(row.payload_json)
            association = _catalog_association(row.payload_json)
            review_only = bool(association.get("review_only"))
            accepted = row.id in selected_raw_row_ids
            status = "accepted" if accepted else (
                "review_neighbour" if review_only else (
                    "ambiguous"
                    if effective_status == "ambiguous"
                    else effective_status
                )
            )
            measurements = _measurement_summaries(session, row.id)
            beams = _measurement_beams(session, row.id)
            attributes = (
                *_catalog_payload_summaries(run.provider, row.payload_json),
                *_attribute_summaries(session, row.id),
            )
            native_pm = _attribute_pm(session, row.id, provider=run.provider)
            ra2000, dec2000, pm_ra, pm_dec, pm_source, note = _display_position_2000(
                row.ra_deg,
                row.dec_deg,
                row.epoch,
                motion_solution,
                native_pm=native_pm,
                base_note=(
                    f"catalog run {run.id}; provider status {effective_status}"
                ),
            )
            uncertainty_major, uncertainty_minor = _position_uncertainty_arcsec(
                run.provider, row.payload_json
            )
            provenance = tuple({
                "role": item.role,
                "service": item.service,
                "catalog_id": item.catalog_id,
                "table_id": item.table_id,
                "row_key": item.row_key,
                "identifier_column": item.identifier_column,
                "identifier_value": item.identifier_value,
                "source_url": item.source_url,
                "access_url": item.access_url,
                "readme_url": item.readme_url,
            } for item in session.scalars(
                select(CatalogDetectionProvenance)
                .where(CatalogDetectionProvenance.detection_id == row.detection_id)
                .order_by(CatalogDetectionProvenance.id)
            ))
            points.append(
                SkyPoint(
                    kind="catalog",
                    provider=run.provider,
                    status=status,
                    source_id=row.source_id,
                    source_display_name=catalog_source_display_name(
                        run.provider, row.source_id, payload
                    ),
                    ra_deg=ra2000,
                    dec_deg=dec2000,
                    separation_arcsec=_separation_arcsec(center, ra2000, dec2000),
                    score=row.score,
                    accepted=accepted,
                    run_id=run.id,
                    raw_row_id=row.id,
                    detection_id=row.detection_id,
                    target_id=target.id,
                    run_target_sdbid=target.sdbid,
                    native_epoch=row.epoch,
                    native_ra_deg=row.ra_deg,
                    native_dec_deg=row.dec_deg,
                    pm_ra_cosdec_masyr=pm_ra,
                    pm_dec_masyr=pm_dec,
                    pm_source=pm_source,
                    photometry=measurements,
                    photometry_beams=beams,
                    attributes=attributes,
                    uncertainty_major_arcsec=uncertainty_major,
                    uncertainty_minor_arcsec=uncertainty_minor,
                    catalog_component=_catalog_component_summary(
                        run.provider, payload, row.source_id,
                    ),
                    provenance=provenance,
                    note=note,
                )
            )
    return points


def _catalog_component_summary(
    provider: str,
    payload: dict[str, object],
    source_id: str,
) -> str | None:
    if provider == "ubvmeans":
        value = decode_ubv_component(payload, source_id)
    elif provider == "tdsc":
        value = decode_tdsc_component(payload, source_id)
    elif provider == "v70a":
        value = decode_v70a_component(payload, source_id)
    else:
        return None
    if value.kind == "named_component":
        if provider == "v70a":
            return (
                f"{value.native_code} — V/70A component "
                f"{value.component_label}"
            )
        return (
            f"{value.native_code} — TDSC component {value.component_label}; "
            "WDS designation where available"
        )
    if value.kind == "component_ordinal":
        return (
            f"{value.native_code} — component {value.component_label} "
            "(ordinal catalogue code)"
        )
    if value.kind == "combined_components":
        return "D — combined light from at least two components; subset unspecified"
    if value.kind == "supplementary_identifier":
        return "S — supplementary identification; component requires review"
    if value.kind == "unknown":
        return f"{value.native_code} — unknown catalogue component code"
    return None


def _annotate_catalog_target_candidates(
    session: Session,
    target: Target,
    center: tuple[float, float],
    points: list[SkyPoint],
    *,
    system_context: dict[str, object],
) -> list[SkyPoint]:
    """Annotate or project detections that reconcile to review targets."""
    candidate_rows = system_context.get("catalog_target_candidates") or []
    if not candidate_rows:
        return points
    strong_by_detection: dict[int, list[dict[str, object]]] = {}
    current_target_rows: dict[int, dict[str, object]] = {}
    for row in candidate_rows:
        detection_id = int(row["detection_id"])
        if row.get("target_sdbid") == target.sdbid:
            # Even an ordinary positional candidate must be actionable.  The
            # source association is the normal ownership decision; the
            # provider-run status only records how the detection was found.
            current_target_rows[detection_id] = row
        if row.get("association_status") not in {
            "current_match", "strong_candidate", "accepted", "rejected",
        }:
            continue
        strong_by_detection.setdefault(detection_id, []).append(row)

    annotated = []
    existing_detection_ids = set()
    for point in points:
        if point.detection_id is None:
            annotated.append(point)
            continue
        existing_detection_ids.add(point.detection_id)
        associations = strong_by_detection.get(point.detection_id, [])
        current_row = current_target_rows.get(point.detection_id)
        if not associations and current_row is None:
            annotated.append(point)
            continue
        linked = tuple(dict.fromkeys(
            str(row["target_sdbid"]) for row in associations
        ))
        reason = _catalog_candidate_reason(
            associations or ([current_row] if current_row is not None else [])
        )
        annotated.append(replace(
            point,
            kind=(
                "catalog_association"
                if current_row is not None
                else point.kind
            ),
            status=(
                str(current_row["association_status"])
                if (
                    current_row is not None
                    and current_row["association_status"]
                    in {"accepted", "rejected"}
                )
                else point.status
            ),
            linked_target_sdbids=tuple(dict.fromkeys(
                (*point.linked_target_sdbids, *linked)
            )),
            cross_candidate_reason=reason,
            note="; ".join(value for value in (point.note, reason) if value),
        ))

    targets = {
        value.id: value
        for value in session.scalars(select(Target).where(
            Target.id.in_({
                int(row["target_id"]) for row in current_target_rows.values()
            })
        ))
    }
    for detection_id, row in current_target_rows.items():
        if detection_id in existing_detection_ids:
            continue
        association_target = targets.get(int(row["target_id"]))
        if association_target is None:
            continue
        motion_solution = _target_motion_solution(session, association_target)
        ra2000, dec2000, pm_ra, pm_dec, pm_source, note = (
            _display_position_2000(
                float(row["ra_deg"]),
                float(row["dec_deg"]),
                float(row["epoch"]),
                motion_solution,
                base_note=(
                    f"catalog detection {detection_id}; "
                    f"{row['association_status']} for {target.sdbid}"
                ),
            )
        )
        encounter_sdbids = tuple(str(value) for value in row.get(
            "encounter_sdbids", []
        ))
        representative_raw_row_id = int(row["representative_raw_row_id"])
        reason = _catalog_candidate_reason([row])
        annotated.append(SkyPoint(
            kind="catalog_association",
            provider=str(row["provider"]),
            status=(
                str(row["association_status"])
                if row["association_status"] in {"accepted", "rejected"}
                else "candidate"
            ),
            source_id=str(row["source_id"]),
            source_display_name=str(row["source_display_name"]),
            ra_deg=ra2000,
            dec_deg=dec2000,
            separation_arcsec=_separation_arcsec(center, ra2000, dec2000),
            score=float(row["score"]),
            run_id=int(row["representative_run_id"]),
            raw_row_id=representative_raw_row_id,
            detection_id=detection_id,
            target_id=target.id,
            run_target_sdbid=(
                None
                if not encounter_sdbids
                else str(row.get("representative_run_target_sdbid") or "")
            ),
            native_epoch=float(row["epoch"]),
            native_ra_deg=float(row["ra_deg"]),
            native_dec_deg=float(row["dec_deg"]),
            pm_ra_cosdec_masyr=pm_ra,
            pm_dec_masyr=pm_dec,
            pm_source=pm_source,
            photometry=_measurement_summaries(
                session, representative_raw_row_id
            ),
            photometry_beams=_measurement_beams(
                session, representative_raw_row_id
            ),
            linked_target_sdbids=(target.sdbid,),
            cross_candidate_reason=reason,
            note="; ".join(value for value in (
                note,
                f"encountered by {', '.join(encounter_sdbids)}"
                if encounter_sdbids else "",
                reason,
            ) if value),
        ))
    return annotated


def _catalog_candidate_reason(
    rows: list[dict[str, object]],
) -> str:
    targets = [
        f"{row['target_sdbid']} ({row['association_basis']}; "
        f"{float(row['separation_arcsec']):.3f} arcsec)"
        for row in rows
    ]
    return "catalog detection reconciles to " + ", ".join(targets)


def _hierarchy_points(
    session: Session,
    target: Target,
    center: tuple[float, float],
) -> tuple[list[SkyPoint], list[SkySegment]]:
    matched_rows = list(session.execute(
        select(HierarchyMatchCandidate, HierarchyRecord)
        .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
        .where(HierarchyMatchCandidate.target_id == target.id)
        .order_by(
            HierarchyMatchCandidate.provider,
            HierarchyRecord.native_id,
            HierarchyRecord.component,
            HierarchyMatchCandidate.score.desc(),
            HierarchyMatchCandidate.id,
        )
    ))
    record_keys = {
        (record.source_id, record.native_id)
        for _candidate, record in matched_rows
    }
    if record_keys:
        source_ids = {source_id for source_id, _native_id in record_keys}
        native_ids = {native_id for _source_id, native_id in record_keys}
        sibling_records = tuple(session.scalars(
            select(HierarchyRecord)
            .where(HierarchyRecord.source_id.in_(source_ids))
            .where(HierarchyRecord.native_id.in_(native_ids))
            .order_by(HierarchyRecord.source_id, HierarchyRecord.native_id, HierarchyRecord.component)
        ))
    else:
        sibling_records = ()
    record_index = {
        (record.source_id, record.native_id, record.component): record
        for record in sibling_records
        if (record.source_id, record.native_id) in record_keys
    }
    matched_record_ids = {record.id for _candidate, record in matched_rows}
    rows: list[tuple[HierarchyMatchCandidate | None, HierarchyRecord]] = [
        *matched_rows,
        *(
            (None, record)
            for record in sibling_records
            if record.id not in matched_record_ids
            and (record.source_id, record.native_id) in record_keys
        ),
    ]
    record_ids = [record.id for _candidate, record in rows]
    graph_edges_by_record: dict[int, list] = {}
    if record_ids:
        graph_edges = tuple(session.scalars(
            select(StructuralEdge)
            .where(StructuralEdge.record_id.in_(record_ids))
            .where(StructuralEdge.status.in_(GRAPH_EDGE_STATUSES))
            .order_by(
                StructuralEdge.source,
                StructuralEdge.native_id,
                StructuralEdge.reference_label,
                StructuralEdge.component_label,
                StructuralEdge.id,
            )
        ))
        graph_overrides = latest_overrides(session, list(graph_edges))
        for edge in graph_edges:
            if edge.record_id is not None:
                graph_edges_by_record.setdefault(edge.record_id, []).append(
                    edge_row(edge, graph_overrides.get(edge.id))
                )
    points: list[SkyPoint] = []
    segments: list[SkySegment] = []
    for candidate, record in rows:
        if record.ra_deg is None or record.dec_deg is None:
            continue
        if _wds_record_has_unusable_separation(record):
            continue
        source_id = _hierarchy_source_id(record)
        display_ra, display_dec, display_position_kind = _hierarchy_display_position(record, center)
        status = candidate.status if candidate is not None else "context"
        candidate_id = candidate.id if candidate is not None else None
        candidate_target_id = target.id if candidate is not None else None
        note_parts = [
            (
                f"hierarchy candidate {candidate.id}"
                if candidate is not None
                else "same hierarchy group; context only"
            ),
            *(
                (f"method {candidate.match_method}",)
                if candidate is not None
                else ()
            ),
            f"record {record.id}",
            f"plotted at {display_position_kind}",
        ]
        if record.discoverer_id:
            note_parts.append(f"discoverer {record.discoverer_id}")
        if record.component:
            note_parts.append(f"component {record.component}")
        if record.measure_epoch is not None:
            note_parts.append(f"epoch {record.measure_epoch:g}")
        if record.separation_arcsec is not None and not _hierarchy_geometry_usable(record):
            note_parts.append(f"rho {record.separation_arcsec:g}\" ignored as unusable WDS sentinel")
        elif record.separation_arcsec is not None:
            note_parts.append(f"rho {record.separation_arcsec:g}\"")
        if record.pa_deg is not None:
            note_parts.append(f"PA {record.pa_deg:g} deg")
        if candidate is not None and candidate.reason:
            note_parts.append(candidate.reason)
        raw_payload = _hierarchy_raw_payload(record)
        unusable_separation = raw_payload.get("unusable_separation_arcsec")
        if unusable_separation is not None:
            note_parts.append(f"rho {float(unusable_separation):g}\" ignored as unusable WDS sentinel")
        raw_component = str(raw_payload.get("Comp") or "").strip()
        raw_reference = str(raw_payload.get("rComp") or "").strip()
        if _wds_blank_component_implies_ab(record, record_index, raw_reference, raw_component):
            raw_reference = "A"
            raw_component = "B"
            note_parts.append("blank WDS component displayed as implicit A-B pair")
        display_reference, display_component = _hierarchy_display_components(
            record.provider,
            raw_reference,
            raw_component,
            record.component,
        )
        display_source_id = _hierarchy_source_id(
            record,
            component_override=display_component if display_component else None,
        )
        if raw_reference:
            note_parts.append(f"relative to component {raw_reference}")
        if display_component and display_component != raw_component:
            note_parts.append(f"displayed endpoint component {display_component}")
        points.append(
            SkyPoint(
                kind="hierarchy",
                provider=record.provider,
                status=status,
                source_id=display_source_id,
                ra_deg=display_ra,
                dec_deg=display_dec,
                separation_arcsec=_separation_arcsec(center, display_ra, display_dec),
                score=candidate.score if candidate is not None else None,
                accepted=status == "accepted",
                candidate_id=candidate_id,
                target_id=candidate_target_id,
                attributes=tuple(_hierarchy_attribute_summaries(record)),
                note="; ".join(note_parts),
            )
        )
        graph_rows = graph_edges_by_record.get(record.id, [])
        if graph_rows:
            for graph_row in graph_rows:
                if (
                    graph_row.start_ra_deg is None
                    or graph_row.start_dec_deg is None
                    or graph_row.end_ra_deg is None
                    or graph_row.end_dec_deg is None
                ):
                    continue
                label = graph_row.component_label or graph_row.source_component or record.component or source_id
                segment_note = (
                    f"{label}: persisted hierarchy graph edge from {graph_row.reference_label or 'primary'}; "
                    f"type {graph_row.relation_type}; role {graph_row.structural_role}; geometry {graph_row.geometry_status}"
                )
                if graph_row.override_id is not None:
                    segment_note += f"; override {graph_row.override_id} by {graph_row.override_actor}: {graph_row.override_reason}"
                segments.append(
                    SkySegment(
                        kind="hierarchy_component_link",
                        provider=graph_row.provider,
                        status=graph_row.status,
                        source_id=display_source_id,
                        label=label,
                        start_ra_deg=graph_row.start_ra_deg,
                        start_dec_deg=graph_row.start_dec_deg,
                        end_ra_deg=graph_row.end_ra_deg,
                        end_dec_deg=graph_row.end_dec_deg,
                        candidate_id=candidate_id,
                        target_id=candidate_target_id,
                        native_id=graph_row.native_id,
                        reference_label=graph_row.reference_label,
                        component_label=graph_row.component_label,
                        relation_type=graph_row.relation_type,
                        structural_role=graph_row.structural_role,
                        note="; ".join((*note_parts, segment_note)),
                    )
                )
            continue
        if record.provider in {"ccdm", "wds"} and raw_component:
            reference_component = display_reference or ("A" if display_component != "A" else "")
            if reference_component:
                anchor = record_index.get((record.source_id, record.native_id, reference_component))
                group_anchor = _wds_group_reference_position(
                    record_index,
                    record,
                    reference_component,
                ) if record.provider == "wds" else None
                if group_anchor is not None:
                    start_ra, start_dec, start_kind = group_anchor
                    if _hierarchy_geometry_usable(record):
                        link_end_ra, link_end_dec = _offset_position(
                            start_ra,
                            start_dec,
                            record.separation_arcsec,
                            record.pa_deg,
                        )
                        link_basis = "WDS reference-group midpoint plus rho/PA"
                    else:
                        link_end_ra, link_end_dec = display_ra, display_dec
                        link_basis = "WDS reference-group midpoint to catalog position"
                elif anchor is not None and anchor.ra_deg is not None and anchor.dec_deg is not None:
                    start_ra, start_dec, start_kind = _hierarchy_component_position(anchor, center)
                    link_end_ra, link_end_dec = display_ra, display_dec
                    link_basis = f"{record.provider.upper()} component positions"
                elif _hierarchy_geometry_usable(record):
                    start_ra, start_dec = record.ra_deg, record.dec_deg
                    start_kind = display_position_kind
                    link_end_ra, link_end_dec = _offset_position(
                        record.ra_deg,
                        record.dec_deg,
                        record.separation_arcsec,
                        record.pa_deg,
                    )
                    link_basis = "rho/PA endpoint"
                else:
                    start_ra = start_dec = link_end_ra = link_end_dec = None
                    start_kind = ""
                    link_basis = ""
                if start_ra is not None and start_dec is not None and link_end_ra is not None and link_end_dec is not None:
                    relation_type = _hierarchy_relation_type(
                        record.provider,
                        reference_component,
                        display_component or raw_component,
                        record.component,
                    )
                    epoch_note = (
                        "CCDM positions are plotted at epoch 2000.0"
                        if record.provider == "ccdm"
                        else "WDS link uses catalog pair geometry/position"
                    )
                    segment_note = (
                        f"{display_component or raw_component}: component link from {reference_component} ({start_kind}); "
                        f"type {relation_type}; {epoch_note}; basis {link_basis}"
                    )
                    if _hierarchy_geometry_usable(record):
                        segment_note += (
                            f"; relative measurement year {record.measure_epoch:g}"
                            if record.measure_epoch is not None else
                            "; relative measurement year unavailable"
                        )
                    else:
                        segment_note += "; no measured rho/PA in this row"
                    segments.append(
                        SkySegment(
                            kind="hierarchy_component_link",
                            provider=record.provider,
                            status=status,
                            source_id=display_source_id,
                            label=display_component or raw_component,
                            start_ra_deg=start_ra,
                            start_dec_deg=start_dec,
                            end_ra_deg=link_end_ra,
                            end_dec_deg=link_end_dec,
                            candidate_id=candidate_id,
                            target_id=candidate_target_id,
                            native_id=record.native_id,
                            reference_label=reference_component,
                            component_label=display_component or raw_component,
                            relation_type=relation_type,
                            note="; ".join((*note_parts, segment_note)),
                        )
                    )
        elif _hierarchy_geometry_usable(record):
            end_ra, end_dec = _offset_position(
                record.ra_deg,
                record.dec_deg,
                record.separation_arcsec,
                record.pa_deg,
            )
            segments.append(
                SkySegment(
                    kind="hierarchy_component_link",
                    provider=record.provider,
                    status=status,
                    source_id=display_source_id,
                    label=display_component or raw_component or record.component or record.discoverer_id or source_id,
                    start_ra_deg=record.ra_deg,
                    start_dec_deg=record.dec_deg,
                    end_ra_deg=end_ra,
                    end_dec_deg=end_dec,
                    candidate_id=candidate_id,
                    target_id=candidate_target_id,
                    native_id=record.native_id,
                    reference_label="primary",
                    component_label=display_component or raw_component or record.component or record.discoverer_id or source_id,
                    relation_type="component",
                    note="; ".join(note_parts),
                )
            )
        elif raw_reference and raw_component:
            anchor = record_index.get((record.source_id, record.native_id, display_reference or raw_reference))
            if anchor is not None and anchor.ra_deg is not None and anchor.dec_deg is not None:
                start_ra, start_dec, start_kind = _hierarchy_component_position(anchor, center)
                relation_type = _hierarchy_relation_type(
                    record.provider,
                    display_reference or raw_reference,
                    display_component or raw_component,
                    record.component,
                )
                segments.append(
                    SkySegment(
                        kind="hierarchy_component_link",
                        provider=record.provider,
                        status=status,
                        source_id=display_source_id,
                        label=display_component or raw_component,
                        start_ra_deg=start_ra,
                        start_dec_deg=start_dec,
                        end_ra_deg=display_ra,
                        end_dec_deg=display_dec,
                        candidate_id=candidate_id,
                        target_id=candidate_target_id,
                        native_id=record.native_id,
                        reference_label=display_reference or raw_reference,
                        component_label=display_component or raw_component,
                        relation_type=relation_type,
                        note="; ".join((
                            *note_parts,
                            f"{display_component or raw_component}: relative-component anchor from {display_reference or raw_reference} ({start_kind}); type {relation_type}; no measured rho/PA in this row",
                        )),
                    )
                )
    return points, segments


def _hierarchy_display_components(
    provider: str,
    raw_reference: str,
    raw_component: str,
    original_component: str | None,
) -> tuple[str, str]:
    """Return review/display reference and endpoint labels.

    WDS stores pair labels such as AB in one field. For plotting, the catalog
    coordinate is the reference/primary side and the rho/PA endpoint is the
    concerned component, so AB is best displayed as A -> B. Preserve the native
    component string in notes/attributes for provenance.
    """
    reference = raw_reference.strip()
    component = raw_component.strip()
    original = (original_component or "").strip()
    if provider == "wds":
        if "," in original:
            left, right = [part.strip() for part in original.split(",", 1)]
            if not reference:
                reference = left
            component = right or component
        compact = component.replace(" ", "")
        if not reference and len(compact) == 2 and compact.isalpha():
            return compact[0], compact[1]
    return reference, component


def _wds_blank_component_implies_ab(
    record: HierarchyRecord,
    record_index: dict[tuple[int, str, str | None], HierarchyRecord],
    raw_reference: str,
    raw_component: str,
) -> bool:
    if record.provider != "wds":
        return False
    if raw_reference.strip() or raw_component.strip() or (record.component or "").strip():
        return False
    if not _hierarchy_geometry_usable(record):
        return False
    return (record.source_id, record.native_id, "AB") not in record_index


def _hierarchy_geometry_usable(record: HierarchyRecord) -> bool:
    if record.separation_arcsec is None or record.pa_deg is None:
        return False
    if record.provider == "wds" and record.separation_arcsec >= UNUSABLE_SEPARATION_ARCSEC:
        return False
    return True


def _wds_record_has_unusable_separation(record: HierarchyRecord) -> bool:
    if record.provider != "wds":
        return False
    if record.separation_arcsec is not None and record.separation_arcsec >= UNUSABLE_SEPARATION_ARCSEC:
        return True
    return _hierarchy_raw_payload(record).get("unusable_separation_arcsec") is not None


def _hierarchy_relation_type(
    provider: str,
    reference_label: str | None,
    component_label: str | None,
    original_component: str | None,
) -> str:
    if provider != "wds":
        return "component"
    reference = (reference_label or "").strip()
    component = (component_label or "").strip()
    if _wds_same_group(reference, component):
        return "internal"
    if _wds_structural_group_reference(reference, component, original_component):
        return "group"
    return "cross_link"


def _wds_same_group(first: str, second: str) -> bool:
    first_group = _wds_component_group(first)
    second_group = _wds_component_group(second)
    return first_group is not None and first_group == second_group and (first != first_group or second != second_group)


def _wds_component_group(label: str | None) -> str | None:
    compact = (label or "").strip().replace(" ", "")
    if not compact:
        return None
    if len(compact) >= 2 and compact[0].isupper() and (compact[1].islower() or compact[1].isdigit()):
        return compact[0]
    return compact


def _wds_structural_group_reference(reference_label: str, component_label: str | None, original_component: str | None) -> bool:
    compact = (reference_label or "").strip().replace(" ", "")
    component = (component_label or "").strip().replace(" ", "")
    if not compact:
        return False
    if len(compact) > 1 and compact.isalpha() and "," in (original_component or ""):
        return True
    if len(compact) == 1 and len(component) == 1 and compact.isalpha() and component.isalpha() and compact != component:
        return True
    return False


def _hierarchy_source_id(
    record: HierarchyRecord,
    *,
    component_override: str | None = None,
) -> str:
    parts = [record.native_id]
    if component_override:
        parts.append(component_override)
    elif record.component:
        parts.append(record.component)
    elif record.discoverer_id:
        parts.append(record.discoverer_id)
    return " ".join(part for part in parts if part)


def _hierarchy_display_position(
    record: HierarchyRecord,
    center: tuple[float, float],
) -> tuple[float, float, str]:
    positions = hierarchy_record_positions(record)
    if not positions:
        raise ValueError("hierarchy record has no display position")
    if record.provider == "wds" and _hierarchy_geometry_usable(record):
        for ra_deg, dec_deg, position_kind in positions:
            if position_kind == "component endpoint":
                return ra_deg, dec_deg, "WDS PA2/Sep2 endpoint"
    return min(
        positions,
        key=lambda value: _separation_arcsec(center, value[0], value[1]),
    )


def _hierarchy_component_position(
    record: HierarchyRecord,
    center: tuple[float, float],
) -> tuple[float, float, str]:
    positions = hierarchy_record_positions(record)
    for ra_deg, dec_deg, position_kind in positions:
        if position_kind == "component endpoint":
            return ra_deg, dec_deg, position_kind
    return min(
        positions,
        key=lambda value: _separation_arcsec(center, value[0], value[1]),
    )


def _wds_group_reference_position(
    record_index: dict[tuple[int, str, str | None], HierarchyRecord],
    record: HierarchyRecord,
    reference_component: str,
) -> tuple[float, float, str] | None:
    compact = reference_component.replace(" ", "")
    if len(compact) != 2 or not compact.isalpha():
        return None
    group_record = record_index.get((record.source_id, record.native_id, compact))
    if (
        group_record is None
        or group_record.ra_deg is None
        or group_record.dec_deg is None
        or not _hierarchy_geometry_usable(group_record)
    ):
        return None
    secondary_ra, secondary_dec = _offset_position(
        group_record.ra_deg,
        group_record.dec_deg,
        group_record.separation_arcsec,
        group_record.pa_deg,
    )
    midpoint_ra, midpoint_dec = _midpoint_position(
        group_record.ra_deg,
        group_record.dec_deg,
        secondary_ra,
        secondary_dec,
    )
    return midpoint_ra, midpoint_dec, f"{compact} midpoint"


def _hierarchy_raw_payload(record: HierarchyRecord) -> dict[str, object]:
    try:
        value = json.loads(record.raw_payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _hierarchy_attribute_summaries(record: HierarchyRecord) -> tuple[str, ...]:
    values = []
    raw_payload = _hierarchy_raw_payload(record)
    if record.discoverer_id:
        values.append(f"discoverer={record.discoverer_id}")
    if record.component:
        values.append(f"component={record.component}")
    if record.separation_arcsec is not None and not _hierarchy_geometry_usable(record):
        values.append(
            f"rho={_compact_display_value(record.separation_arcsec)} arcsec unusable"
        )
    elif record.separation_arcsec is not None:
        values.append(f"rho={_compact_display_value(record.separation_arcsec)} arcsec")
    elif raw_payload.get("unusable_separation_arcsec") is not None:
        values.append(
            "rho="
            f"{_compact_display_value(float(raw_payload['unusable_separation_arcsec']))} "
            "arcsec unusable"
        )
    if record.pa_deg is not None:
        values.append(f"pa={_compact_display_value(record.pa_deg)} deg")
    if record.measure_epoch is not None:
        values.append(f"epoch={_compact_display_value(record.measure_epoch)}")
    if record.magnitude_primary is not None:
        values.append(f"mag1={_compact_display_value(record.magnitude_primary)}")
    if record.magnitude_secondary is not None:
        values.append(f"mag2={_compact_display_value(record.magnitude_secondary)}")
    return tuple(values)


def _display_position_2000(
    ra_deg: float,
    dec_deg: float,
    epoch: float,
    solution: AstrometricSolution | Astrometry | None,
    *,
    native_pm: tuple[float, float, str] | None = None,
    base_note: str,
) -> tuple[float, float, float | None, float | None, str | None, str]:
    pm_ra = None
    pm_dec = None
    pm_source = None
    if native_pm is not None:
        pm_ra, pm_dec, pm_source = native_pm
    elif (
        solution is not None
        and solution.proper_motion_available
        and solution.pm_ra_cosdec_masyr is not None
        and solution.pm_dec_masyr is not None
    ):
        pm_ra = solution.pm_ra_cosdec_masyr
        pm_dec = solution.pm_dec_masyr
        pm_source = f"assumed target PM ({solution.source})"
    if math.isclose(epoch, 2000.0):
        return ra_deg, dec_deg, pm_ra, pm_dec, pm_source, f"{base_note}; plotted at epoch 2000.0"
    if pm_ra is None or pm_dec is None:
        return ra_deg, dec_deg, None, None, None, (
            f"{base_note}; native epoch {epoch:g}, plotted without propagation because no target PM is available"
        )
    propagated = propagate_to_epoch(
        Astrometry(
            ra_deg,
            dec_deg,
            epoch,
            pm_ra_cosdec_masyr=pm_ra,
            pm_dec_masyr=pm_dec,
            source="review",
        ),
        2000.0,
    )
    qualifier = (
        "using native source PM"
        if native_pm is not None
        else "using target PM as counterpart hypothesis"
    )
    return propagated.ra_deg, propagated.dec_deg, pm_ra, pm_dec, pm_source, (
        f"{base_note}; native epoch {epoch:g}, plotted at epoch 2000.0 {qualifier}"
    )


def _measurement_summaries(session: Session, raw_row_id: int, *, limit: int = 8) -> tuple[str, ...]:
    raw = session.get(RawCatalogRow, raw_row_id)
    if raw is None:
        return ()
    rows = session.scalars(
        select(NormalizedMeasurement)
        .where(NormalizedMeasurement.detection_id == raw.detection_id)
        .order_by(NormalizedMeasurement.band)
    )
    summaries = []
    for measurement in rows:
        marker = "<" if measurement.upper_limit else ""
        error = (
            f" ± {_compact_display_value(measurement.error)}"
            if measurement.error else ""
        )
        flags = []
        if measurement.quality:
            flags.append(str(measurement.quality))
        if measurement.excluded:
            flags.append("excluded")
        if measurement.blend_state != "clear":
            flags.append(measurement.blend_state)
        suffix = f" ({', '.join(flags)})" if flags else ""
        summaries.append(
            f"{measurement.band}={marker}{_compact_display_value(measurement.value)}"
            f"{error} {measurement.unit}{suffix}"
        )
        if len(summaries) >= limit:
            break
    return tuple(summaries)


def _measurement_beams(session: Session, raw_row_id: int, *, limit: int = 12) -> tuple[PhotometryBeam, ...]:
    raw = session.get(RawCatalogRow, raw_row_id)
    if raw is None:
        return ()
    rows = session.scalars(
        select(NormalizedMeasurement)
        .where(NormalizedMeasurement.detection_id == raw.detection_id)
        .where(NormalizedMeasurement.resolution_major_arcsec.is_not(None))
        .order_by(
            NormalizedMeasurement.provider,
            NormalizedMeasurement.resolution_major_arcsec,
            NormalizedMeasurement.band,
        )
    )
    beams = []
    for measurement in rows:
        if measurement.resolution_major_arcsec is None:
            continue
        beams.append(
            PhotometryBeam(
                provider=measurement.provider,
                band=measurement.band,
                major_arcsec=measurement.resolution_major_arcsec,
                minor_arcsec=measurement.resolution_minor_arcsec,
                kind=measurement.resolution_kind,
                reference=measurement.resolution_reference,
                ownership_scope=measurement.ownership_scope,
                blend_state=measurement.blend_state,
                blend_reason=measurement.blend_reason,
                value=measurement.value,
                error=measurement.error,
                unit=measurement.unit,
                upper_limit=measurement.upper_limit,
            )
        )
        if len(beams) >= limit:
            break
    return tuple(beams)


def _catalog_payload(payload_json: str) -> dict[str, object] | None:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _catalog_association(payload_json: str) -> dict[str, object]:
    payload = _catalog_payload(payload_json)
    if payload is None:
        return {}
    association = payload.get("_sdb_association")
    return association if isinstance(association, dict) else {}


def _compact_display_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            absolute = abs(number)
            if absolute == 0 or absolute >= 0.01:
                return f"{number:.2f}"
            decimals = min(10, max(3, math.ceil(-math.log10(absolute)) + 1))
            return f"{number:.{decimals}f}"
    return str(value)


def _catalog_payload_summaries(
    provider: str, payload_json: str, *, limit: int = 8
) -> tuple[str, ...]:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    payload = normalize_review_payload(provider, payload)
    values: list[str] = []
    association = payload.get("_sdb_association")
    if isinstance(association, dict):
        if association.get("review_only"):
            values.append("review-only catalogue neighbour")
        candidate_separation = association.get("candidate_separation_arcsec")
        acceptance_radius = association.get("acceptance_radius_arcsec")
        query_radius = association.get("query_radius_arcsec")
        if candidate_separation is not None:
            values.append(
                f"candidate separation={_compact_display_value(candidate_separation)} arcsec"
            )
        if acceptance_radius is not None:
            values.append(
                f"acceptance radius={_compact_display_value(acceptance_radius)} arcsec"
            )
        if query_radius is not None:
            values.append(
                f"query radius={_compact_display_value(query_radius)} arcsec"
            )
        if association.get("identifier_agreement"):
            values.append("identifier agrees with target aliases")
    review = payload.get("_sdb_review")
    fields = review.get("fields", ()) if isinstance(review, dict) else ()
    for field in fields if isinstance(fields, list) else ():
        if not isinstance(field, dict):
            continue
        value = field.get("value")
        if value is None or str(value).strip() == "":
            continue
        label = field.get("label") or field.get("key") or "catalog attribute"
        source = field.get("source_column")
        unit = field.get("unit")
        source_text = f" ({source})" if source else ""
        unit_text = f" {unit}" if unit else ""
        values.append(
            f"{label}{source_text}={_compact_display_value(value)}{unit_text}"
        )
        if len(values) >= limit:
            break
    return tuple(values)




def _attribute_summaries(session: Session, raw_row_id: int, *, limit: int = 8) -> tuple[str, ...]:
    rows = session.scalars(
        select(CatalogAttribute)
        .where(CatalogAttribute.raw_row_id == raw_row_id)
        .order_by(CatalogAttribute.key)
    )
    summaries = []
    for attribute in rows:
        value = attribute.value_text if attribute.value_text is not None else attribute.value_float
        if value is None:
            continue
        error = (
            f" ± {_compact_display_value(attribute.uncertainty)}"
            if attribute.uncertainty is not None else ""
        )
        unit = f" {attribute.unit}" if attribute.unit else ""
        quality = f" [{attribute.quality}]" if attribute.quality else ""
        summaries.append(
            f"{attribute.key}={_compact_display_value(value)}{error}{unit}{quality}"
        )
        if len(summaries) >= limit:
            break
    return tuple(summaries)


def _attribute_pm(
    session: Session, raw_row_id: int, *, provider: str
) -> tuple[float, float, str] | None:
    # Older AllWISE imports used these generic keys for the catalog's
    # short-baseline apparent-motion fit. Never reinterpret those historical
    # attributes as stellar proper motion; refreshed rows use explicit
    # ``apparent_motion_*`` keys instead.
    if provider == "allwise":
        return None
    rows = list(session.scalars(
        select(CatalogAttribute).where(CatalogAttribute.raw_row_id == raw_row_id)
    ))
    values = {row.key: row for row in rows}
    pm_ra = values.get("pm_ra_cosdec")
    pm_dec = values.get("pm_dec")
    if pm_ra is None or pm_dec is None or pm_ra.value_float is None or pm_dec.value_float is None:
        return None
    source = pm_ra.reference or pm_dec.reference or "native catalog PM"
    return pm_ra.value_float, pm_dec.value_float, source


def _position_uncertainty_arcsec(
    provider: str, payload_json: str
) -> tuple[float | None, float | None]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    payload = normalize_review_payload(provider, payload)
    review = payload.get("_sdb_review")
    uncertainty = review.get("position_uncertainty") if isinstance(review, dict) else None
    if not isinstance(uncertainty, dict):
        return None, None
    try:
        major = float(uncertainty["major_arcsec"])
        minor = float(uncertainty["minor_arcsec"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not all(math.isfinite(value) and value > 0 for value in (major, minor)):
        return None, None
    return major, minor


def _simbad_metadata_points(session: Session, target: Target, center: tuple[float, float]) -> list[SkyPoint]:
    rows = session.scalars(
        select(SimbadMetadata)
        .where(SimbadMetadata.target_id == target.id)
        .order_by(SimbadMetadata.id.desc())
        .limit(1)
    )
    solution = _target_solution(session, target)
    use_solution_pm = (
        solution is not None
        and solution.source == "simbad"
        and solution.proper_motion_available
        and solution.pm_ra_cosdec_masyr is not None
        and solution.pm_dec_masyr is not None
    )
    points = []
    for row in rows:
        note = "current/latest SIMBAD metadata position"
        pm_ra = None
        pm_dec = None
        pm_source = None
        if row.pm_ra_cosdec_masyr is not None and row.pm_dec_masyr is not None:
            pm_ra = row.pm_ra_cosdec_masyr
            pm_dec = row.pm_dec_masyr
            pm_source = row.proper_motion_bibcode or "simbad metadata"
            note += "; PM from SIMBAD metadata"
        elif use_solution_pm:
            pm_ra = solution.pm_ra_cosdec_masyr
            pm_dec = solution.pm_dec_masyr
            pm_source = "canonical simbad astrometry"
            note += "; PM from canonical SIMBAD astrometric solution"
        points.append(
            SkyPoint(
                kind="metadata",
                provider="simbad",
                status="match",
                source_id=row.main_id,
                ra_deg=row.ra_deg,
                dec_deg=row.dec_deg,
                separation_arcsec=_separation_arcsec(center, row.ra_deg, row.dec_deg),
                accepted=True,
                run_id=row.run_id,
                pm_ra_cosdec_masyr=pm_ra,
                pm_dec_masyr=pm_dec,
                pm_source=pm_source,
                note=note,
            )
        )
    return points


def _separation_arcsec(center: tuple[float, float], ra_deg: float, dec_deg: float) -> float:
    x, y = _offset_arcsec(center, ra_deg, dec_deg)
    return math.hypot(x, y)


def _segment_farthest_offset(center: tuple[float, float], segment: SkySegment) -> float:
    start = _offset_arcsec(center, segment.start_ra_deg, segment.start_dec_deg)
    end = _offset_arcsec(center, segment.end_ra_deg, segment.end_dec_deg)
    return max(math.hypot(*start), math.hypot(*end))


def _offset_position(
    ra_deg: float,
    dec_deg: float,
    separation_arcsec: float,
    pa_deg: float,
) -> tuple[float, float]:
    pa = math.radians(pa_deg)
    east_arcsec = separation_arcsec * math.sin(pa)
    north_arcsec = separation_arcsec * math.cos(pa)
    cos_dec = max(0.01, abs(math.cos(math.radians(dec_deg))))
    return (
        (ra_deg + east_arcsec / (3600.0 * cos_dec)) % 360.0,
        dec_deg + north_arcsec / 3600.0,
    )


def _midpoint_position(
    first_ra_deg: float,
    first_dec_deg: float,
    second_ra_deg: float,
    second_dec_deg: float,
) -> tuple[float, float]:
    dra = second_ra_deg - first_ra_deg
    if dra > 180.0:
        dra -= 360.0
    elif dra < -180.0:
        dra += 360.0
    return (
        (first_ra_deg + dra / 2.0) % 360.0,
        (first_dec_deg + second_dec_deg) / 2.0,
    )


def _offset_arcsec(center: tuple[float, float], ra_deg: float, dec_deg: float) -> tuple[float, float]:
    ra0, dec0 = center
    dra = ra_deg - ra0
    if dra > 180.0:
        dra -= 360.0
    elif dra < -180.0:
        dra += 360.0
    x = dra * math.cos(math.radians(dec0)) * 3600.0
    y = (dec_deg - dec0) * 3600.0
    return x, y
