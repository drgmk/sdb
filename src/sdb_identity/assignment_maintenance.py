"""Read-only audits for explicit measurement-attribution development data."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .effective_assignments import derived_measurement_assignments
from .models import MeasurementTargetAssociation


@dataclass(frozen=True)
class AutomaticAssignmentAudit:
    method: str
    row_count: int
    measurement_count: int
    redundant_measurement_ids: tuple[int, ...]
    redundant_row_count: int
    classification_counts: dict[str, int]


def audit_automatic_assignment_sets(
    session: Session,
    *,
    method: str = "automatic_proposal",
) -> AutomaticAssignmentAudit:
    """Classify stored automatic sets without changing current state.

    A set is redundant only when every explicit row for the measurement came
    from ``method`` and its complete target/role/weight signature exactly
    matches the safe source-association default.  Mixed-method and genuine
    composite/shared/per-band exceptions are intentionally retained.
    """

    rows = list(session.scalars(
        select(MeasurementTargetAssociation)
        .order_by(
            MeasurementTargetAssociation.measurement_id,
            MeasurementTargetAssociation.id,
        )
    ))
    by_measurement: dict[int, list[MeasurementTargetAssociation]] = defaultdict(list)
    for row in rows:
        by_measurement[row.measurement_id].append(row)
    candidate_ids = {
        measurement_id
        for measurement_id, values in by_measurement.items()
        if any(row.method == method for row in values)
    }
    derived_by_measurement = defaultdict(list)
    for row in derived_measurement_assignments(session, candidate_ids):
        derived_by_measurement[row.measurement_id].append(row)

    classifications: Counter[str] = Counter()
    redundant_ids = []
    redundant_row_count = 0
    for measurement_id in sorted(candidate_ids):
        explicit = by_measurement[measurement_id]
        if any(row.method != method for row in explicit):
            classifications["mixed_methods"] += 1
            continue
        derived = derived_by_measurement.get(measurement_id, [])
        if not derived:
            classifications["no_safe_default"] += 1
            continue
        explicit_signature = {
            (row.target_id, row.role, row.weight) for row in explicit
        }
        derived_signature = {
            (row.target_id, row.role, row.weight) for row in derived
        }
        if explicit_signature != derived_signature:
            classifications["explicit_exception"] += 1
            continue
        classifications["redundant_default"] += 1
        redundant_ids.append(measurement_id)
        redundant_row_count += len(explicit)

    return AutomaticAssignmentAudit(
        method=method,
        row_count=sum(
            row.method == method for row in rows
        ),
        measurement_count=len(candidate_ids),
        redundant_measurement_ids=tuple(redundant_ids),
        redundant_row_count=redundant_row_count,
        classification_counts=dict(sorted(classifications.items())),
    )
