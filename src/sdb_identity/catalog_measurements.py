from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CatalogRun, NormalizedMeasurement, RawCatalogRow


@dataclass(frozen=True)
class CurrentMeasurementEncounter:
    target_id: int
    measurement: NormalizedMeasurement
    raw_row: RawCatalogRow
    run: CatalogRun


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
    query = (
        select(NormalizedMeasurement, RawCatalogRow, CatalogRun)
        .join(
            RawCatalogRow,
            RawCatalogRow.detection_id == NormalizedMeasurement.detection_id,
        )
        .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
        .where(
            CatalogRun.target_id.in_(ids),
            CatalogRun.is_current.is_(True),
            RawCatalogRow.accepted.is_(True),
        )
        .order_by(
            CatalogRun.target_id,
            NormalizedMeasurement.provider,
            NormalizedMeasurement.source_id,
            NormalizedMeasurement.band,
            NormalizedMeasurement.id,
            RawCatalogRow.id.desc(),
        )
    )
    if require_match:
        query = query.where(CatalogRun.status == "match")
    result: dict[tuple[int, int], CurrentMeasurementEncounter] = {}
    for measurement, raw_row, run in session.execute(query):
        result.setdefault(
            (run.target_id, measurement.id),
            CurrentMeasurementEncounter(run.target_id, measurement, raw_row, run),
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
