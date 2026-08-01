from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog_results import (
    effective_catalog_results,
    effective_catalog_selected_rows,
)
from .models.catalogs import (
    CatalogDetection,
    CatalogRun,
    CatalogTargetAssociationAction,
    NormalizedMeasurement,
    RawCatalogRow,
)
from .vocabulary import ProviderRunStatus


@dataclass(frozen=True)
class CurrentMeasurementEncounter:
    target_id: int
    measurement: NormalizedMeasurement
    raw_row: RawCatalogRow
    run: CatalogRun


def current_catalog_detection_target_pairs(
    session: Session,
    detection_ids: Iterable[int] | None = None,
    *,
    require_match: bool = True,
) -> set[tuple[int, int]]:
    """Return effective canonical-detection/target links."""
    effective = _effective_detection_target_ids(
        session, detection_ids, require_match=require_match,
    )
    return set(effective)


def _identifier_agreement_rank(
    raw_row: RawCatalogRow,
    provider: str,
) -> int:
    try:
        payload = json.loads(raw_row.payload_json)
    except (TypeError, json.JSONDecodeError):
        return 0
    association = payload.get("_sdb_association") if isinstance(payload, dict) else None
    if not (
        isinstance(association, dict)
        and association.get("identifier_agreement") is True
    ):
        return 0
    if provider != "tdsc":
        return 2
    component = str(payload.get("m_TDSC") or "").strip().upper()
    matched = [
        str(value).strip().upper()
        for value in association.get("matched_identifiers") or []
    ]
    if any(value.startswith("TYC ") for value in matched):
        return 2
    if component and any(
        value.startswith(("HD ", "WDS "))
        and value.endswith(component)
        for value in matched
    ):
        return 2
    # HIP and an unsuffixed HD/WDS name identify the multiple system, not the
    # specific TDSC component.
    return 1


def _effective_detection_target_ids(
    session: Session,
    detection_ids: Iterable[int] | None,
    *,
    require_match: bool,
) -> set[tuple[int, int]]:
    """Resolve current target encounters, preferring native identifier evidence.

    A detection found by several target queries is associated only with the
    identifier-backed target(s) when those exist. Audited accept/reject actions
    are then applied as explicit overrides.
    """
    ids = (
        None
        if detection_ids is None
        else tuple(dict.fromkeys(int(value) for value in detection_ids))
    )
    if ids == ():
        return set()
    target_query = select(CatalogRun.target_id).join(
        RawCatalogRow, RawCatalogRow.run_id == CatalogRun.id,
    ).where(CatalogRun.is_current.is_(True))
    if ids is not None:
        target_query = target_query.where(RawCatalogRow.detection_id.in_(ids))
    target_ids = set(session.scalars(target_query))
    implicit: dict[int, dict[int, int]] = {}
    for result_row in effective_catalog_results(session, target_ids).values():
        if require_match and result_row.status != ProviderRunStatus.MATCH:
            continue
        for raw_row, detection in effective_catalog_selected_rows(
            session, result_row,
        ):
            if ids is not None and detection.id not in ids:
                continue
            target_rows = implicit.setdefault(detection.id, {})
            target_rows[result_row.target_id] = max(
                target_rows.get(result_row.target_id, 0),
                _identifier_agreement_rank(
                    raw_row, result_row.provider,
                ),
            )
    result: set[tuple[int, int]] = set()
    for detection_id, target_rows in implicit.items():
        strongest = max(target_rows.values(), default=0)
        identifier_targets = {
            target_id
            for target_id, rank in target_rows.items()
            if rank == strongest and strongest > 0
        }
        selected = identifier_targets or set(target_rows)
        result.update((detection_id, target_id) for target_id in selected)

    latest_actions: dict[tuple[int, int], CatalogTargetAssociationAction] = {}
    action_query = select(CatalogTargetAssociationAction)
    if ids is not None:
        action_query = action_query.where(
            CatalogTargetAssociationAction.detection_id.in_(ids)
        )
    for action in session.scalars(
        action_query.order_by(CatalogTargetAssociationAction.id)
    ):
        latest_actions[(action.detection_id, action.target_id)] = action
    for key, action in latest_actions.items():
        if action.action == "accept":
            result.add(key)
        else:
            result.discard(key)
    return result


def current_measurement_encounters(
    session: Session,
    target_ids: Iterable[int],
    *,
    require_match: bool = True,
) -> list[CurrentMeasurementEncounter]:
    """Return canonical measurements encountered by current target runs.

    A normalized measurement belongs to a provider detection, while the raw
    row records that a particular target query encountered and accepted it.
    The result is unique per target and canonical measurement even if an
    interrupted or malformed database contains redundant current encounters.
    """
    ids = tuple(dict.fromkeys(int(value) for value in target_ids))
    if not ids:
        return []
    latest_actions: dict[
        tuple[int, int], CatalogTargetAssociationAction
    ] = {}
    for action in session.scalars(
        select(CatalogTargetAssociationAction)
        .where(CatalogTargetAssociationAction.target_id.in_(ids))
        .order_by(CatalogTargetAssociationAction.id)
    ):
        latest_actions[(action.target_id, action.detection_id)] = action

    queried_rows = []
    for result_row in effective_catalog_results(session, ids).values():
        if result_row.status != ProviderRunStatus.MATCH:
            continue
        for raw_row, detection in effective_catalog_selected_rows(
            session, result_row,
        ):
            for measurement in session.scalars(
                select(NormalizedMeasurement)
                .where(
                    NormalizedMeasurement.detection_id == detection.id
                )
                .order_by(
                    NormalizedMeasurement.provider,
                    NormalizedMeasurement.source_id,
                    NormalizedMeasurement.band,
                    NormalizedMeasurement.id,
                )
            ):
                queried_rows.append((measurement, raw_row, result_row.run))
    effective_targets = _effective_detection_target_ids(
        session,
        {measurement.detection_id for measurement, _raw, _run in queried_rows},
        require_match=require_match,
    )
    result: dict[tuple[int, int], CurrentMeasurementEncounter] = {}
    for measurement, raw_row, run in queried_rows:
        action = latest_actions.get((run.target_id, measurement.detection_id))
        if action is not None and action.action == "reject":
            continue
        if (
            measurement.detection_id,
            run.target_id,
        ) not in effective_targets:
            continue
        result.setdefault(
            (run.target_id, measurement.id),
            CurrentMeasurementEncounter(run.target_id, measurement, raw_row, run),
        )

    accepted_actions = [
        action
        for action in latest_actions.values()
        if action.action == "accept"
    ]
    if accepted_actions:
        action_by_id = {action.id: action for action in accepted_actions}
        accepted_rows = session.execute(
            select(
                CatalogTargetAssociationAction,
                NormalizedMeasurement,
                RawCatalogRow,
                CatalogRun,
            )
            .join(
                NormalizedMeasurement,
                NormalizedMeasurement.detection_id
                == CatalogTargetAssociationAction.detection_id,
            )
            .join(
                RawCatalogRow,
                RawCatalogRow.id
                == CatalogTargetAssociationAction.reviewed_raw_row_id,
            )
            .join(
                CatalogRun,
                CatalogRun.id
                == CatalogTargetAssociationAction.reviewed_run_id,
            )
            .where(
                CatalogTargetAssociationAction.id.in_(action_by_id),
                RawCatalogRow.detection_id
                == CatalogTargetAssociationAction.detection_id,
                RawCatalogRow.run_id
                == CatalogTargetAssociationAction.reviewed_run_id,
            )
            .order_by(
                CatalogTargetAssociationAction.target_id,
                NormalizedMeasurement.provider,
                NormalizedMeasurement.source_id,
                NormalizedMeasurement.band,
                NormalizedMeasurement.id,
            )
        )
        for action, measurement, raw_row, run in accepted_rows:
            result.setdefault(
                (action.target_id, measurement.id),
                CurrentMeasurementEncounter(
                    action.target_id, measurement, raw_row, run,
                ),
            )
    return list(result.values())


def current_measurements_for_target(
    session: Session,
    target_id: int,
    *,
    require_match: bool = True,
) -> list[NormalizedMeasurement]:
    return [
        row.measurement
        for row in current_measurement_encounters(
            session, [target_id], require_match=require_match,
        )
    ]


def current_measurement_target_ids(
    session: Session,
    measurement_ids: Iterable[int],
) -> dict[int, set[int]]:
    """Return all effective target associations for canonical measurements."""
    ids = tuple(dict.fromkeys(int(value) for value in measurement_ids))
    if not ids:
        return {}
    detection_ids = set(session.scalars(
        select(NormalizedMeasurement.detection_id).where(
            NormalizedMeasurement.id.in_(ids)
        )
    ))
    if not detection_ids:
        return {}
    target_ids = set(session.scalars(
        select(CatalogRun.target_id)
        .join(RawCatalogRow, RawCatalogRow.run_id == CatalogRun.id)
        .where(RawCatalogRow.detection_id.in_(detection_ids))
    ))
    target_ids.update(session.scalars(
        select(CatalogTargetAssociationAction.target_id).where(
            CatalogTargetAssociationAction.detection_id.in_(detection_ids)
        )
    ))
    selected_ids = set(ids)
    result: dict[int, set[int]] = {}
    for encounter in current_measurement_encounters(session, target_ids):
        if encounter.measurement.id in selected_ids:
            result.setdefault(encounter.measurement.id, set()).add(
                encounter.target_id
            )
    return result
