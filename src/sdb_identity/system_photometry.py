"""Reusable factual state for system-level photometry projections."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .catalog_measurements import (
    CurrentMeasurementEncounter,
    current_catalog_detection_target_pairs,
    current_measurement_encounters,
)
from .effective_assignments import (
    EffectiveMeasurementAssignment,
    effective_measurement_assignments,
)
from .measurement_eligibility import (
    EffectiveMeasurementEligibility,
    effective_measurement_eligibility,
)
from .models.catalogs import (
    CatalogDetection,
    CatalogDetectionProvenance,
    NormalizedMeasurement,
    RawCatalogRow,
)
from .models.photometry import MeasurementTargetAssociation
from .models.identity import Target
from .models.hierarchy import TargetLifecycleAction, TargetSystem, TargetSystemMember
from .vocabulary import TargetRole, TargetState


@dataclass(frozen=True)
class TargetLifecycleState:
    """Effective lifecycle state, including the action that established it."""

    role: TargetRole
    state: TargetState
    action_id: int | None
    superseded_by_target_id: int | None


@dataclass(frozen=True)
class SystemMembershipState:
    """One target's membership in a named physical system."""

    system_id: int
    name: str
    component_label: str | None
    source: str
    primary: bool


@dataclass(frozen=True)
class SystemPhotometryState:
    """Shared facts from which photometry-specific views are projected.

    The state deliberately contains no review classification, fitting group,
    readiness severity, or export formatting.  Those remain consumer-specific
    projections over the same current database facts.
    """

    selected_target_ids: frozenset[int]
    context_target_ids: frozenset[int]
    targets: Mapping[int, Target]
    referenced_targets: Mapping[int, Target]
    lifecycle: Mapping[int, TargetLifecycleState]
    system_memberships: Mapping[int, tuple[SystemMembershipState, ...]]
    detections: Mapping[int, CatalogDetection]
    measurements: Mapping[int, NormalizedMeasurement]
    encounters: tuple[CurrentMeasurementEncounter, ...]
    encounter_target_ids: Mapping[int, frozenset[int]]
    raw_rows: Mapping[int, RawCatalogRow]
    raw_payloads: Mapping[int, Mapping[str, Any]]
    catalog_provenance: Mapping[
        int, tuple[CatalogDetectionProvenance, ...]
    ]
    detection_target_ids: Mapping[int, frozenset[int]]
    assignments: tuple[EffectiveMeasurementAssignment, ...]
    eligibility: Mapping[int, EffectiveMeasurementEligibility]

    def invariant_errors(self) -> tuple[str, ...]:
        """Return structural inconsistencies in the loaded fact set."""

        errors = []
        if not self.selected_target_ids <= self.context_target_ids:
            errors.append("selected targets are missing from context")
        if set(self.targets) != set(self.context_target_ids):
            errors.append("context target rows do not match context target ids")
        measurement_ids = set(self.measurements)
        if set(self.eligibility) != measurement_ids:
            errors.append("measurement eligibility is incomplete")
        if {
            measurement.detection_id
            for measurement in self.measurements.values()
        } - set(self.detections):
            errors.append("canonical detection rows are incomplete")
        if any(
            encounter.target_id not in self.context_target_ids
            or encounter.measurement.id not in measurement_ids
            or encounter.raw_row.id not in self.raw_rows
            for encounter in self.encounters
        ):
            errors.append("current encounters reference unloaded facts")
        if any(
            assignment.measurement_id not in measurement_ids
            for assignment in self.assignments
        ):
            errors.append("effective assignments reference unloaded measurements")
        if any(
            assignment.target_id not in self.referenced_targets
            for assignment in self.assignments
        ):
            errors.append("effective assignments reference unloaded targets")
        if any(
            detection_id not in self.detections
            for detection_id in self.catalog_provenance
        ):
            errors.append("catalog provenance references unloaded detections")
        return tuple(errors)


def load_system_photometry_state(
    session: Session,
    selected_target_ids: Iterable[int],
    *,
    expand_context: bool = True,
    require_match: bool = True,
) -> SystemPhotometryState:
    """Load reusable current photometry facts for selected targets.

    With ``expand_context`` enabled, system relatives and targets connected by
    explicit measurement attribution are followed to a fixed point.  Export
    and sample readiness use selected-only state; fitting and review use the
    expanded context.
    """

    selected = frozenset(int(value) for value in selected_target_ids)
    if not selected:
        return _empty_state()

    if expand_context:
        context_ids, assigned_measurement_ids = _context_closure(
            session, set(selected)
        )
    else:
        context_ids = set(selected)
        assigned_measurement_ids = set()

    encounters = tuple(_current_encounters(
        session, context_ids, require_match=require_match,
    ))
    encounter_target_ids_mutable: dict[int, set[int]] = defaultdict(set)
    for encounter in encounters:
        encounter_target_ids_mutable[encounter.measurement.id].add(
            encounter.target_id
        )

    measurement_ids = (
        assigned_measurement_ids
        | set(encounter_target_ids_mutable)
    )
    targets = {
        row.id: row
        for row in _scalars_in(session, Target, Target.id, context_ids)
    }
    measurements = {
        row.id: row
        for row in _scalars_in(
            session,
            NormalizedMeasurement,
            NormalizedMeasurement.id,
            measurement_ids,
        )
    }
    detection_ids = {
        measurement.detection_id for measurement in measurements.values()
    }
    detections = {
        row.id: row
        for row in _scalars_in(
            session, CatalogDetection, CatalogDetection.id, detection_ids
        )
    }

    raw_row_ids = {
        encounter.raw_row.id for encounter in encounters
    } | {
        measurement.raw_row_id for measurement in measurements.values()
        if measurement.raw_row_id is not None
    }
    raw_rows = {
        row.id: row
        for row in _scalars_in(
            session, RawCatalogRow, RawCatalogRow.id, raw_row_ids
        )
    }
    raw_payloads = {}
    for raw_row_id, raw_row in raw_rows.items():
        try:
            payload = json.loads(raw_row.payload_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            raw_payloads[raw_row_id] = payload

    provenance_mutable: dict[
        int, list[CatalogDetectionProvenance]
    ] = defaultdict(list)
    for row in _scalars_in(
        session,
        CatalogDetectionProvenance,
        CatalogDetectionProvenance.detection_id,
        detection_ids,
    ):
        provenance_mutable[row.detection_id].append(row)
    catalog_provenance = {
        detection_id: tuple(sorted(rows, key=lambda row: row.id))
        for detection_id, rows in provenance_mutable.items()
    }

    detection_targets_mutable: dict[int, set[int]] = defaultdict(set)
    for detection_id, target_id in current_catalog_detection_target_pairs(
        session, detection_ids, require_match=require_match,
    ):
        detection_targets_mutable[detection_id].add(target_id)

    assignments = tuple(
        effective_measurement_assignments(session, measurement_ids)
    )
    eligibility = effective_measurement_eligibility(
        session, measurement_ids
    )
    lifecycle = _lifecycle(session, context_ids)
    referenced_target_ids = context_ids | {
        measurement.target_id for measurement in measurements.values()
    } | {
        assignment.target_id for assignment in assignments
    } | {
        target_id
        for target_ids in detection_targets_mutable.values()
        for target_id in target_ids
    } | {
        row.superseded_by_target_id
        for row in lifecycle.values()
        if row.superseded_by_target_id is not None
    }
    referenced_targets = {
        row.id: row
        for row in _scalars_in(
            session, Target, Target.id, referenced_target_ids
        )
    }
    return SystemPhotometryState(
        selected_target_ids=selected,
        context_target_ids=frozenset(context_ids),
        targets=targets,
        referenced_targets=referenced_targets,
        lifecycle=lifecycle,
        system_memberships=_system_memberships(session, context_ids),
        detections=detections,
        measurements=measurements,
        encounters=encounters,
        encounter_target_ids={
            measurement_id: frozenset(target_ids)
            for measurement_id, target_ids
            in encounter_target_ids_mutable.items()
        },
        raw_rows=raw_rows,
        raw_payloads=raw_payloads,
        catalog_provenance=catalog_provenance,
        detection_target_ids={
            detection_id: frozenset(target_ids)
            for detection_id, target_ids
            in detection_targets_mutable.items()
        },
        assignments=assignments,
        eligibility=eligibility,
    )


def _empty_state() -> SystemPhotometryState:
    return SystemPhotometryState(
        selected_target_ids=frozenset(),
        context_target_ids=frozenset(),
        targets={},
        referenced_targets={},
        lifecycle={},
        system_memberships={},
        detections={},
        measurements={},
        encounters=(),
        encounter_target_ids={},
        raw_rows={},
        raw_payloads={},
        catalog_provenance={},
        detection_target_ids={},
        assignments=(),
        eligibility={},
    )


def _context_closure(
    session: Session,
    selected_target_ids: set[int],
) -> tuple[set[int], set[int]]:
    target_ids = set(selected_target_ids)
    measurement_ids: set[int] = set()
    changed = True
    while changed:
        system_ids = set()
        for chunk in _chunks(target_ids):
            system_ids.update(session.scalars(
                select(TargetSystemMember.system_id).where(
                    TargetSystemMember.target_id.in_(chunk)
                )
            ))
        new_targets = set()
        for chunk in _chunks(system_ids):
            new_targets.update(session.scalars(
                select(TargetSystemMember.target_id).where(
                    TargetSystemMember.system_id.in_(chunk)
                )
            ))
        frontier_targets = target_ids | new_targets
        new_measurements = set()
        for chunk in _chunks(frontier_targets):
            new_measurements.update(session.scalars(
                select(MeasurementTargetAssociation.measurement_id).where(
                    MeasurementTargetAssociation.target_id.in_(chunk)
                )
            ))
        all_measurements = measurement_ids | new_measurements
        associated_targets = set()
        for chunk in _chunks(all_measurements):
            associated_targets.update(session.scalars(
                select(MeasurementTargetAssociation.target_id).where(
                    MeasurementTargetAssociation.measurement_id.in_(chunk)
                )
            ))
        expanded_targets = frontier_targets | associated_targets
        changed = (
            expanded_targets != target_ids
            or all_measurements != measurement_ids
        )
        target_ids = expanded_targets
        measurement_ids = all_measurements
    return target_ids, measurement_ids


def _current_encounters(
    session: Session,
    target_ids: set[int],
    *,
    require_match: bool,
) -> list[CurrentMeasurementEncounter]:
    result: dict[tuple[int, int], CurrentMeasurementEncounter] = {}
    for chunk in _chunks(target_ids):
        for row in current_measurement_encounters(
            session, chunk, require_match=require_match,
        ):
            result[(row.target_id, row.measurement.id)] = row
    return [
        result[key]
        for key in sorted(result)
    ]


def _lifecycle(
    session: Session,
    target_ids: set[int],
) -> dict[int, TargetLifecycleState]:
    result = {
        target_id: TargetLifecycleState(
            role=TargetRole.UNSPECIFIED,
            state=TargetState.ACTIVE,
            action_id=None,
            superseded_by_target_id=None,
        )
        for target_id in target_ids
    }
    for chunk in _chunks(target_ids):
        latest = (
            select(
                TargetLifecycleAction.target_id,
                func.max(TargetLifecycleAction.id).label("action_id"),
            )
            .where(TargetLifecycleAction.target_id.in_(chunk))
            .group_by(TargetLifecycleAction.target_id)
            .subquery()
        )
        for row in session.scalars(
            select(TargetLifecycleAction).join(
                latest, TargetLifecycleAction.id == latest.c.action_id
            )
        ):
            result[row.target_id] = TargetLifecycleState(
                role=TargetRole.parse(row.role, "target role"),
                state=TargetState.parse(row.state, "target state"),
                action_id=row.id,
                superseded_by_target_id=row.superseded_by_target_id,
            )
    return result


def _system_memberships(
    session: Session,
    target_ids: set[int],
) -> dict[int, tuple[SystemMembershipState, ...]]:
    result: dict[int, list[SystemMembershipState]] = defaultdict(list)
    for chunk in _chunks(target_ids):
        rows = session.execute(
            select(TargetSystemMember, TargetSystem)
            .join(
                TargetSystem,
                TargetSystem.id == TargetSystemMember.system_id,
            )
            .where(TargetSystemMember.target_id.in_(chunk))
            .order_by(TargetSystem.name, TargetSystemMember.id)
        )
        for member, system in rows:
            result[member.target_id].append(SystemMembershipState(
                system_id=system.id,
                name=system.name,
                component_label=member.component_label,
                source=member.source,
                primary=system.primary_target_id == member.target_id,
            ))
    return {
        target_id: tuple(rows)
        for target_id, rows in result.items()
    }


def _scalars_in(
    session: Session,
    model: type,
    column: Any,
    values: set[int],
) -> list[Any]:
    rows = []
    for chunk in _chunks(values):
        rows.extend(session.scalars(select(model).where(column.in_(chunk))))
    return rows


def _chunks(values: Iterable[int], size: int = 500) -> Iterable[list[int]]:
    ordered = sorted(set(values))
    for start in range(0, len(ordered), size):
        yield ordered[start:start + size]
