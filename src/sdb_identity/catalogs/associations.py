from __future__ import annotations

import json
import math
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .policy import (
    catalog_position_matches_components,
    catalog_source_display_name,
    catalog_source_id_matches_identifiers,
)
from ..astrometry import angular_separation_arcsec
from .measurements import current_catalog_detection_target_pairs
from .results import effective_catalog_results
from ..models.identity import ExternalIdentifier, Target
from ..models.catalogs import (
    CatalogDetection,
    CatalogDetectionProvenance,
    CatalogRun,
    CatalogTargetAssociationAction,
    NormalizedMeasurement,
    RawCatalogRow,
)
from ..providers import Astrometry
from ..target_astrometry import best_target_astrometry_map
from ..vocabulary import PROVIDER_FAILURE_STATUSES


DEFAULT_CATALOG_MATCH_RADIUS_ARCSEC = 2.0
DEFAULT_CATALOG_REVIEW_RADIUS_ARCSEC = 15.0


def catalog_coverage_by_target(
    session: Session,
    target_ids: Iterable[int],
    *,
    providers: Iterable[str] | None = None,
) -> list[dict[str, object]]:
    """Report direct, current catalog coverage for each supplied target.

    Coverage is intentionally target-local: a detection encountered while
    querying another system member is useful association evidence, but it does
    not prove that the provider search footprint for this target was covered.
    A current ``match``, ``ambiguous``, or ``no_match`` result all count as
    coverage. Failed latest attempts remain visible but do not count.
    """
    ids = tuple(dict.fromkeys(int(value) for value in target_ids))
    if not ids:
        return []
    targets = list(session.scalars(
        select(Target).where(Target.id.in_(ids)).order_by(Target.id)
    ))
    runs = list(session.scalars(
        select(CatalogRun)
        .where(CatalogRun.target_id.in_(ids))
        .order_by(CatalogRun.id)
    ))
    expected = tuple(dict.fromkeys(
        str(value) for value in (
            providers
            if providers is not None
            else sorted({run.provider for run in runs})
        )
    ))
    latest_by_pair = {
        (run.target_id, run.provider): run
        for run in runs
        if run.provider in expected
    }
    current_by_pair = effective_catalog_results(
        session, ids, providers=expected,
    )
    normalization_by_target: dict[int, dict[int, CatalogDetection]] = {}
    for detection, run in session.execute(
        select(CatalogDetection, CatalogRun)
        .join(
            RawCatalogRow,
            RawCatalogRow.detection_id == CatalogDetection.id,
        )
        .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
        .outerjoin(
            CatalogDetectionProvenance,
            CatalogDetectionProvenance.detection_id == CatalogDetection.id,
        )
        .where(
            CatalogRun.target_id.in_(ids),
            CatalogRun.provider.in_(expected),
            CatalogRun.is_current.is_(True),
            or_(
                CatalogDetection.normalization_status.in_(("pending", "failed")),
                CatalogDetectionProvenance.id.is_(None),
            ),
        )
        .order_by(CatalogRun.target_id, CatalogDetection.id)
    ):
        normalization_by_target.setdefault(run.target_id, {})[
            detection.id
        ] = detection
    result = []
    for target in targets:
        current = tuple(
            provider
            for provider in expected
            if (target.id, provider) in current_by_pair
        )
        missing = tuple(
            provider for provider in expected if provider not in current
        )
        failed = tuple(
            provider
            for provider in missing
            if (
                (latest := latest_by_pair.get((target.id, provider))) is not None
                and latest.status in PROVIDER_FAILURE_STATUSES
            )
        )
        normalization_gaps = [
            {
                "detection_id": detection.id,
                "provider": detection.provider,
                "source_id": detection.source_id,
                "status": (
                    detection.normalization_status
                    if detection.normalization_status in {"pending", "failed"}
                    else "missing_provenance"
                ),
                "error": detection.normalization_error,
            }
            for detection in normalization_by_target.get(target.id, {}).values()
        ]
        result.append({
            "target_id": target.id,
            "target_sdbid": target.sdbid,
            "expected_providers": list(expected),
            "current_providers": list(current),
            "missing_providers": list(missing),
            "failed_providers": list(failed),
            "untried_providers": [
                provider for provider in missing if provider not in failed
            ],
            "current_status_by_provider": {
                provider: current_by_pair[(target.id, provider)].status.value
                for provider in current
            },
            "latest_failure_by_provider": {
                provider: latest_by_pair[(target.id, provider)].error
                for provider in failed
            },
            "normalization_gaps": normalization_gaps,
            "normalization_gap_count": len(normalization_gaps),
            "covered_count": len(current),
            "expected_count": len(expected),
            "complete": not missing,
        })
    return result


def catalog_target_candidates(
    session: Session,
    target_ids: Iterable[int],
    *,
    match_radius_arcsec: float = DEFAULT_CATALOG_MATCH_RADIUS_ARCSEC,
    review_radius_arcsec: float = DEFAULT_CATALOG_REVIEW_RADIUS_ARCSEC,
) -> list[dict[str, object]]:
    """Re-evaluate current catalog detections against a target neighbourhood.

    Raw catalog rows record query encounters.  They do not determine which
    target a provider-native detection belongs to.  This derived graph applies
    the same positional and identifier evidence to every supplied target, so
    adding a component after its parent was queried does not hide evidence that
    was already ingested.

    The result is deliberately read-only.  ``current_match`` mirrors an
    existing accepted encounter; ``strong_candidate`` and ``candidate`` are
    review evidence and do not change catalog or measurement state.
    """
    ids = tuple(dict.fromkeys(int(value) for value in target_ids))
    if not ids:
        return []
    targets = {
        target.id: target
        for target in session.scalars(
            select(Target).where(Target.id.in_(ids)).order_by(Target.id)
        )
    }
    if not targets:
        return []
    astrometry = _target_astrometry(session, targets)
    identifiers = _target_identifiers(session, ids)
    association_actions = _latest_association_actions(session, ids)
    encounters = _current_detection_encounters(
        session, ids, association_actions=association_actions,
    )
    effective_results = effective_catalog_results(session, ids)
    selected_raw_ids = {
        value.selected_raw_row.id
        for value in effective_results.values()
        if value.selected_raw_row is not None
    }
    effective_pairs = current_catalog_detection_target_pairs(
        session, encounters,
    )
    measurement_bands = _detection_measurement_bands(
        session, tuple(encounters)
    )

    result = []
    for detection_id, values in encounters.items():
        detection = values[0][0]
        payload = _payload(detection.payload_json)
        encounter_target_ids = sorted({run.target_id for _detection, _raw, run in values})
        raw_row_ids = sorted(raw.id for _detection, raw, _run in values)
        run_ids = sorted({run.id for _detection, _raw, run in values})
        accepted_target_ids = {
            target_id
            for current_detection_id, target_id in effective_pairs
            if current_detection_id == detection_id
        }
        representative = min(
            values,
            key=lambda value: (
                not (
                    value[1].accepted
                    or value[1].id in selected_raw_ids
                ),
                -value[1].score,
                value[1].id,
            ),
        )
        representative_raw = representative[1]
        representative_run = representative[2]
        source_position = Astrometry(
            detection.ra_deg,
            detection.dec_deg,
            detection.epoch,
            source=detection.provider,
            source_id=detection.source_id,
        )
        for target_id, target in targets.items():
            association_action = association_actions.get(
                (target_id, detection_id)
            )
            separation = angular_separation_arcsec(
                astrometry[target_id],
                source_position,
                epoch=detection.epoch,
            )
            identifier_match = catalog_source_id_matches_identifiers(
                detection.provider,
                detection.source_id,
                identifiers.get(target_id, ()),
                payload=payload,
            )
            current_match = target_id in accepted_target_ids
            position_can_identify_component = catalog_position_matches_components(
                detection.provider
            )
            if (
                not current_match
                and association_action is None
                and not identifier_match
                and (
                    not position_can_identify_component
                    or separation > review_radius_arcsec
                )
            ):
                continue
            if association_action is not None:
                status = (
                    "accepted"
                    if association_action.action == "accept"
                    else "rejected"
                )
                basis = "manual review"
            elif current_match:
                status = "current_match"
                basis = "current catalog match"
            elif identifier_match:
                status = "strong_candidate"
                basis = "catalog identifier"
            elif (
                position_can_identify_component
                and separation <= match_radius_arcsec
            ):
                status = "strong_candidate"
                basis = "close position"
            else:
                status = "candidate"
                basis = "nearby position"
            positional_score = math.exp(
                -0.5 * (separation / match_radius_arcsec) ** 2
            )
            score = 1.0 if identifier_match else positional_score
            if current_match:
                current_scores = [
                    raw.score
                    for _detection, raw, run in values
                    if (
                        (
                            run.target_id == target_id
                            and (
                                raw.accepted
                                or raw.id in selected_raw_ids
                            )
                        )
                        or (
                            association_action is not None
                            and raw.id
                            == association_action.reviewed_raw_row_id
                        )
                    )
                ]
                if current_scores:
                    score = max(score, max(current_scores))
            result.append({
                "detection_id": detection_id,
                "provider": detection.provider,
                "release": detection.release,
                "source_id": detection.source_id,
                "source_display_name": catalog_source_display_name(
                    detection.provider,
                    detection.source_id,
                    payload,
                ),
                "ra_deg": detection.ra_deg,
                "dec_deg": detection.dec_deg,
                "epoch": detection.epoch,
                "target_id": target_id,
                "target_sdbid": target.sdbid,
                "association_status": status,
                "association_basis": basis,
                "association_action_id": (
                    None
                    if association_action is None
                    else association_action.id
                ),
                "association_actor": (
                    None
                    if association_action is None
                    else association_action.actor
                ),
                "association_reason": (
                    None
                    if association_action is None
                    else association_action.reason
                ),
                "separation_arcsec": separation,
                "score": score,
                "identifier_match": identifier_match,
                "measurement_count": len(
                    measurement_bands.get(detection_id, ())
                ),
                "measurement_bands": list(
                    measurement_bands.get(detection_id, ())
                ),
                "encounter_target_ids": encounter_target_ids,
                "encounter_sdbids": sorted(
                    targets[value].sdbid
                    for value in encounter_target_ids
                    if value in targets
                ),
                "run_ids": run_ids,
                "raw_row_ids": raw_row_ids,
                "representative_run_id": representative_run.id,
                "representative_raw_row_id": representative_raw.id,
                "representative_run_target_id": representative_run.target_id,
                "representative_run_target_sdbid": (
                    targets[representative_run.target_id].sdbid
                    if representative_run.target_id in targets
                    else None
                ),
            })
    return sorted(
        result,
        key=lambda row: (
            int(row["target_id"]),
            str(row["provider"]),
            float(row["separation_arcsec"]),
            str(row["source_id"]),
            int(row["detection_id"]),
        ),
    )


def _current_detection_encounters(
    session: Session,
    target_ids: tuple[int, ...],
    *,
    association_actions: dict[
        tuple[int, int], CatalogTargetAssociationAction
    ] | None = None,
) -> dict[int, list[tuple[CatalogDetection, RawCatalogRow, CatalogRun]]]:
    rows = session.execute(
        select(CatalogDetection, RawCatalogRow, CatalogRun)
        .join(RawCatalogRow, RawCatalogRow.detection_id == CatalogDetection.id)
        .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
        .where(
            CatalogRun.target_id.in_(target_ids),
            CatalogRun.is_current.is_(True),
        )
        .order_by(
            CatalogDetection.id,
            CatalogRun.target_id,
            RawCatalogRow.id,
        )
    )
    result: dict[
        int, list[tuple[CatalogDetection, RawCatalogRow, CatalogRun]]
    ] = {}
    for detection, raw, run in rows:
        result.setdefault(detection.id, []).append((detection, raw, run))
    action_rows = association_actions or {}
    known_raw_ids = {
        raw.id
        for values in result.values()
        for _detection, raw, _run in values
    }
    for action in action_rows.values():
        if action.reviewed_raw_row_id in known_raw_ids:
            continue
        row = session.execute(
            select(CatalogDetection, RawCatalogRow, CatalogRun)
            .join(
                RawCatalogRow,
                RawCatalogRow.detection_id == CatalogDetection.id,
            )
            .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
            .where(
                CatalogDetection.id == action.detection_id,
                RawCatalogRow.id == action.reviewed_raw_row_id,
                CatalogRun.id == action.reviewed_run_id,
            )
        ).one_or_none()
        if row is None:
            continue
        detection, raw, run = row
        result.setdefault(detection.id, []).append((detection, raw, run))
        known_raw_ids.add(raw.id)
    return result


def _latest_association_actions(
    session: Session,
    target_ids: tuple[int, ...],
) -> dict[tuple[int, int], CatalogTargetAssociationAction]:
    if not target_ids:
        return {}
    result: dict[
        tuple[int, int], CatalogTargetAssociationAction
    ] = {}
    for action in session.scalars(
        select(CatalogTargetAssociationAction)
        .where(CatalogTargetAssociationAction.target_id.in_(target_ids))
        .order_by(CatalogTargetAssociationAction.id)
    ):
        result[(action.target_id, action.detection_id)] = action
    return result


def _detection_measurement_bands(
    session: Session,
    detection_ids: tuple[int, ...],
) -> dict[int, tuple[str, ...]]:
    if not detection_ids:
        return {}
    values: dict[int, list[str]] = {}
    for detection_id, band in session.execute(
        select(
            NormalizedMeasurement.detection_id,
            NormalizedMeasurement.band,
        )
        .where(NormalizedMeasurement.detection_id.in_(detection_ids))
        .order_by(
            NormalizedMeasurement.detection_id,
            NormalizedMeasurement.band,
            NormalizedMeasurement.id,
        )
    ):
        values.setdefault(detection_id, []).append(band)
    return {
        detection_id: tuple(bands)
        for detection_id, bands in values.items()
    }


def _target_identifiers(
    session: Session,
    target_ids: tuple[int, ...],
) -> dict[int, tuple[str, ...]]:
    values: dict[int, list[str]] = {target_id: [] for target_id in target_ids}
    for row in session.scalars(
        select(ExternalIdentifier)
        .where(ExternalIdentifier.target_id.in_(target_ids))
        .order_by(ExternalIdentifier.target_id, ExternalIdentifier.id)
    ):
        values[row.target_id].append(row.value)
    return {target_id: tuple(rows) for target_id, rows in values.items()}


def _target_astrometry(
    session: Session,
    targets: dict[int, Target],
) -> dict[int, Astrometry]:
    return best_target_astrometry_map(session, targets.values())


def _payload(value: str | None) -> dict[str, object]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
