"""Effective catalog results over acquisition evidence and decisions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    CatalogAttribute,
    CatalogDetection,
    CatalogResultDecision,
    CatalogRun,
    NormalizedMeasurement,
    RawCatalogRow,
)
from .vocabulary import ProviderRunStatus


@dataclass(frozen=True)
class EffectiveCatalogResult:
    target_id: int
    provider: str
    run: CatalogRun
    status: ProviderRunStatus
    selected_detection: CatalogDetection | None
    selected_raw_row: RawCatalogRow | None
    decision: CatalogResultDecision | None

    @property
    def selected_source_id(self) -> str | None:
        if self.selected_detection is not None:
            return self.selected_detection.source_id
        return self.run.selected_source_id


def effective_catalog_selected_rows(
    session: Session,
    result: EffectiveCatalogResult,
) -> tuple[tuple[RawCatalogRow, CatalogDetection], ...]:
    """Return every selected detection represented by an effective result.

    Ordinary positional catalog runs select one row, but curated dataset runs
    can natively accept several independent detections. A review decision over
    an ambiguous run deliberately selects exactly its referenced detection.
    """

    if result.status != ProviderRunStatus.MATCH:
        return ()
    if result.decision is not None:
        if (
            result.selected_raw_row is None
            or result.selected_detection is None
        ):
            return ()
        return ((result.selected_raw_row, result.selected_detection),)
    rows = tuple(session.scalars(
        select(RawCatalogRow)
        .where(
            RawCatalogRow.run_id == result.run.id,
            RawCatalogRow.accepted.is_(True),
        )
        .order_by(RawCatalogRow.id)
    ))
    return tuple(
        (row, detection)
        for row in rows
        if (
            detection := session.get(CatalogDetection, row.detection_id)
        ) is not None
    )


def effective_catalog_results(
    session: Session,
    target_ids: Iterable[int],
    *,
    providers: Iterable[str] | None = None,
) -> dict[tuple[int, str], EffectiveCatalogResult]:
    """Return one effective current result per target/provider."""

    ids = tuple(dict.fromkeys(int(value) for value in target_ids))
    if not ids:
        return {}
    provider_values = (
        None
        if providers is None
        else tuple(dict.fromkeys(str(value) for value in providers))
    )
    query = select(CatalogRun).where(
        CatalogRun.target_id.in_(ids),
        CatalogRun.is_current.is_(True),
    )
    if provider_values is not None:
        query = query.where(CatalogRun.provider.in_(provider_values))
    runs = list(session.scalars(query.order_by(CatalogRun.id)))
    decisions: dict[int, CatalogResultDecision] = {}
    run_ids = {run.id for run in runs}
    if run_ids:
        decision_query = (
            select(CatalogResultDecision)
            .where(CatalogResultDecision.reviewed_run_id.in_(run_ids))
            .order_by(CatalogResultDecision.id)
        )
        for decision in session.scalars(decision_query):
            decisions[decision.reviewed_run_id] = decision
    result: dict[tuple[int, str], EffectiveCatalogResult] = {}
    for run in runs:
        decision = decisions.get(run.id)
        status = ProviderRunStatus.parse(run.status, "catalog status")
        raw_row = None
        detection = None
        if decision is not None:
            status = (
                ProviderRunStatus.MATCH
                if decision.action == "accept_detection"
                else ProviderRunStatus.NO_MATCH
            )
            if decision.reviewed_raw_row_id is not None:
                raw_row = session.get(
                    RawCatalogRow, decision.reviewed_raw_row_id,
                )
            if decision.accepted_detection_id is not None:
                detection = session.get(
                    CatalogDetection, decision.accepted_detection_id,
                )
        elif status == ProviderRunStatus.MATCH:
            raw_row = session.scalar(
                select(RawCatalogRow)
                .where(
                    RawCatalogRow.run_id == run.id,
                    RawCatalogRow.accepted.is_(True),
                )
                .order_by(RawCatalogRow.id)
                .limit(1)
            )
            detection = None if raw_row is None else session.get(CatalogDetection, raw_row.detection_id)
        result[(run.target_id, run.provider)] = EffectiveCatalogResult(
            run.target_id,
            run.provider,
            run,
            status,
            detection,
            raw_row,
            decision,
        )
    return result


def catalog_run_signature(
    session: Session,
    run: CatalogRun | None,
    *,
    effective_status: str | ProviderRunStatus | None = None,
    selected_source_id: str | None = None,
    selected_raw_row_id: int | None = None,
):
    """Comparable provider evidence used to decide whether export became dirty."""
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
    ).join(
        RawCatalogRow,
        RawCatalogRow.detection_id == NormalizedMeasurement.detection_id,
    ).where(
        RawCatalogRow.run_id == run.id,
        (
            RawCatalogRow.id == selected_raw_row_id
            if selected_raw_row_id is not None
            else RawCatalogRow.accepted.is_(True)
        ),
    ).order_by(NormalizedMeasurement.id)).all())
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
        run.selected_source_id if selected_source_id is None else selected_source_id,
        rows,
        measurements,
        attributes,
    )
