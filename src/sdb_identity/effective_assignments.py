from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .catalog_measurements import current_measurement_target_ids
from .models import MeasurementTargetAssociation, TargetLifecycleAction
from .vocabulary import (
    INACTIVE_TARGET_STATES,
    MeasurementTargetRole,
    TargetRole,
    TargetState,
)


@dataclass(frozen=True)
class EffectiveMeasurementAssignment:
    """Current photometry attribution, whether stored or safely derived."""

    measurement_id: int
    target_id: int
    role: str
    method: str
    weight: float | None
    note: str | None
    association_id: int | None
    derived: bool


def effective_measurement_assignments(
    session: Session,
    measurement_ids: Iterable[int],
) -> list[EffectiveMeasurementAssignment]:
    """Return explicit overrides or an unambiguous catalog-association default.

    Any stored assignment makes the complete assignment set for that
    measurement explicit. Otherwise a measurement encountered for exactly one
    active target inherits that target: physical/unspecified targets contribute
    flux, while composite targets provide scope only. Multiple encountered
    targets deliberately remain unassigned for review.
    """
    ids = tuple(dict.fromkeys(int(value) for value in measurement_ids))
    if not ids:
        return []

    explicit = list(session.scalars(
        select(MeasurementTargetAssociation)
        .where(MeasurementTargetAssociation.measurement_id.in_(ids))
        .order_by(
            MeasurementTargetAssociation.measurement_id,
            MeasurementTargetAssociation.id,
        )
    ))
    explicit_measurement_ids = {row.measurement_id for row in explicit}
    result = [
        EffectiveMeasurementAssignment(
            measurement_id=row.measurement_id,
            target_id=row.target_id,
            role=row.role,
            method=row.method,
            weight=row.weight,
            note=row.note,
            association_id=row.id,
            derived=False,
        )
        for row in explicit
    ]

    encounter_target_ids = current_measurement_target_ids(session, ids)
    candidates = {
        measurement_id: tuple(sorted(set(
            int(target_id)
            for target_id in encounter_target_ids.get(measurement_id, ())
        )))
        for measurement_id in ids
        if measurement_id not in explicit_measurement_ids
    }
    default_target_ids = {
        target_ids[0]
        for target_ids in candidates.values()
        if len(target_ids) == 1
    }
    lifecycle = _latest_lifecycle(session, default_target_ids)
    for measurement_id, target_ids in candidates.items():
        if len(target_ids) != 1:
            continue
        target_id = target_ids[0]
        status = lifecycle.get(target_id)
        role = TargetRole.UNSPECIFIED if status is None else status.role
        state = TargetState.ACTIVE if status is None else status.state
        if state in INACTIVE_TARGET_STATES:
            continue
        assignment_role = (
            MeasurementTargetRole.COMPOSITE_SCOPE
            if role == TargetRole.COMPOSITE
            else MeasurementTargetRole.CONTRIBUTOR
        )
        result.append(EffectiveMeasurementAssignment(
            measurement_id=measurement_id,
            target_id=target_id,
            role=assignment_role.value,
            method="catalog_association_default",
            weight=None,
            note="Derived from one accepted catalog-source association",
            association_id=None,
            derived=True,
        ))
    return sorted(
        result,
        key=lambda row: (
            row.measurement_id,
            row.role,
            row.target_id,
            row.association_id or 0,
        ),
    )


def _latest_lifecycle(
    session: Session,
    target_ids: set[int],
) -> dict[int, TargetLifecycleAction]:
    if not target_ids:
        return {}
    latest = (
        select(
            TargetLifecycleAction.target_id,
            func.max(TargetLifecycleAction.id).label("action_id"),
        )
        .where(TargetLifecycleAction.target_id.in_(target_ids))
        .group_by(TargetLifecycleAction.target_id)
        .subquery()
    )
    return {
        row.target_id: row
        for row in session.scalars(
            select(TargetLifecycleAction).join(
                latest,
                TargetLifecycleAction.id == latest.c.action_id,
            )
        )
    }
