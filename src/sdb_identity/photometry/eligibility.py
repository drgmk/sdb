"""One effective fit/export eligibility projection for normalized measurements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalogs.measurements import (
    current_catalog_detection_target_pairs,
    current_measurement_encounters,
)
from ..models.catalogs import (
    IrasBandSelection,
    IrasSourceFamily,
    NormalizedMeasurement,
)
from ..models.photometry import MeasurementEligibilityAction


@dataclass(frozen=True)
class EffectiveMeasurementEligibility:
    measurement_id: int
    excluded: bool
    basis: str
    reason: str | None
    action_id: int | None = None


def effective_measurement_eligibility(
    session: Session,
    measurement_ids: Iterable[int],
) -> dict[int, EffectiveMeasurementEligibility]:
    """Project manual, structural, and provider-native eligibility.

    Precedence is the latest manual action, then named structural safety
    policies, then the provider-native value stored on the measurement.
    """
    ids = tuple(dict.fromkeys(int(value) for value in measurement_ids))
    if not ids:
        return {}
    measurements = {
        row.id: row
        for row in session.scalars(
            select(NormalizedMeasurement).where(
                NormalizedMeasurement.id.in_(ids)
            )
        )
    }
    missing = set(ids) - set(measurements)
    if missing:
        raise KeyError(
            "measurement not found: "
            + ", ".join(str(value) for value in sorted(missing))
        )

    detection_targets: dict[int, set[int]] = {}
    for detection_id, target_id in current_catalog_detection_target_pairs(
        session,
        {measurement.detection_id for measurement in measurements.values()},
    ):
        detection_targets.setdefault(detection_id, set()).add(target_id)

    relevant_target_ids = {
        target_id
        for measurement in measurements.values()
        for target_id in detection_targets.get(measurement.detection_id, ())
    }
    tdsc_measurements: dict[int, NormalizedMeasurement] = {}
    tdsc_target_ids: dict[int, set[int]] = {}
    if relevant_target_ids:
        for encounter in current_measurement_encounters(
            session, relevant_target_ids,
        ):
            measurement = encounter.measurement
            if measurement.provider != "tdsc":
                continue
            tdsc_measurements[measurement.id] = measurement
            tdsc_target_ids.setdefault(measurement.id, set()).add(
                encounter.target_id
            )

    actions = _latest_actions(
        session, {*measurements, *tdsc_measurements},
    )
    tdsc_preferred = {
        (target_id, measurement.band)
        for measurement_id, measurement in tdsc_measurements.items()
        if not _manual_or_provider_excluded(
            measurement, actions.get(measurement_id)
        )
        for target_id in tdsc_target_ids.get(measurement_id, ())
    }
    iras_alternate_ids = set(session.scalars(
        select(IrasBandSelection.alternate_measurement_id)
        .join(
            IrasSourceFamily,
            IrasSourceFamily.id == IrasBandSelection.family_id,
        )
    ))

    result = {}
    for measurement_id, measurement in measurements.items():
        action = actions.get(measurement_id)
        if action is not None:
            result[measurement_id] = EffectiveMeasurementEligibility(
                measurement_id,
                action.excluded,
                (
                    "manual_exclude_action"
                    if action.excluded
                    else "manual_include_action"
                ),
                action.reason,
                action.id,
            )
            continue
        targets = detection_targets.get(measurement.detection_id, set())
        if len(targets) > 1:
            result[measurement_id] = EffectiveMeasurementEligibility(
                measurement_id,
                True,
                "shared_detection",
                "shared catalog source; component fit/export excluded",
            )
            continue
        if measurement_id in iras_alternate_ids:
            result[measurement_id] = EffectiveMeasurementEligibility(
                measurement_id,
                True,
                "iras_alternate",
                "alternate PSC/FSC measurement",
            )
            continue
        if (
            measurement.provider == "tycho2"
            and any(
                (target_id, measurement.band) in tdsc_preferred
                for target_id in targets
            )
        ):
            result[measurement_id] = EffectiveMeasurementEligibility(
                measurement_id,
                True,
                "tdsc_preferred",
                "TDSC component photometry preferred",
            )
            continue
        result[measurement_id] = EffectiveMeasurementEligibility(
            measurement_id,
            measurement.excluded,
            "provider_excluded" if measurement.excluded else "included",
            measurement.exclusion_reason,
        )
    return result


def _latest_actions(
    session: Session,
    measurement_ids: set[int],
) -> dict[int, MeasurementEligibilityAction]:
    if not measurement_ids:
        return {}
    result = {}
    for action in session.scalars(
        select(MeasurementEligibilityAction)
        .where(
            MeasurementEligibilityAction.measurement_id.in_(measurement_ids)
        )
        .order_by(MeasurementEligibilityAction.id)
    ):
        result[action.measurement_id] = action
    return result


def _manual_or_provider_excluded(
    measurement: NormalizedMeasurement,
    action: MeasurementEligibilityAction | None,
) -> bool:
    return measurement.excluded if action is None else action.excluded
