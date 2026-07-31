"""Audited operator decisions over immutable catalog acquisition evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .catalog_types import CatalogAdapter, CatalogRefreshResult
from .catalog_ingestion import store_catalog_attributes
from .decisions import DecisionContext
from .dirty import mark_export_dirty
from .models import (
    CatalogDetection,
    CatalogResultDecision,
    CatalogRetryAction,
    CatalogRun,
    NormalizedMeasurement,
    RawCatalogRow,
)
from .vocabulary import PROVIDER_FAILURE_STATUSES, ProviderRunStatus


class CatalogAcquisition(Protocol):
    def refresh(self, target_reference: str | int, provider: str) -> CatalogRefreshResult: ...


class CatalogDecisionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapters: Mapping[str, CatalogAdapter] | None = None,
        *,
        acquisition: CatalogAcquisition | None = None,
    ):
        self.sessions = session_factory
        self.adapters = dict(adapters or {})
        self.acquisition = acquisition

    def accept_candidate(
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
                measurement_count = session.query(NormalizedMeasurement).where(
                    NormalizedMeasurement.detection_id == selected_raw.detection_id
                ).count()
                return _result(
                    previous, ProviderRunStatus.MATCH, measurement_count,
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
                raise ValueError(
                    f"catalog adapter cannot reconstruct candidates: {previous.provider}"
                )
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Selected {previous.provider} source {candidate.source_id} "
                    f"for target {previous.target_id}"
                ),
            )
            detection = session.get(CatalogDetection, selected_raw.detection_id)
            if detection is None:
                raise RuntimeError("catalog candidate has no canonical detection")
            measurement_count = session.query(NormalizedMeasurement).where(
                NormalizedMeasurement.detection_id == detection.id
            ).count()
            store_catalog_attributes(
                session,
                candidate,
                run_id=previous.id,
                target_id=previous.target_id,
                raw_row_id=selected_raw.id,
                provider=previous.provider,
            )
            action = CatalogResultDecision(
                target_id=previous.target_id,
                provider=previous.provider,
                reviewed_run_id=previous.id,
                action="accept_detection",
                accepted_detection_id=detection.id,
                reviewed_raw_row_id=selected_raw.id,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(action)
            session.flush()
            _reconcile_iras(session, previous)
            mark_export_dirty(
                session,
                previous.target_id,
                source_type="catalog_result_decision",
                source_id=action.id,
                reason="manual catalog candidate selection",
            )
            return _result(
                previous, ProviderRunStatus.MATCH, measurement_count,
                candidate.source_id,
            )

    def reviewed_no_match(
        self,
        run_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> CatalogRefreshResult:
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
            if latest_decision is not None and latest_decision.action == "reviewed_no_match":
                return _result(previous, ProviderRunStatus.NO_MATCH, 0)
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
            _reconcile_iras(session, previous)
            mark_export_dirty(
                session,
                previous.target_id,
                source_type="catalog_result_decision",
                source_id=action.id,
                reason="reviewed catalog no-match",
            )
            return _result(previous, ProviderRunStatus.NO_MATCH, 0)

    def retry_failed_run(
        self,
        run_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> CatalogRefreshResult:
        if self.acquisition is None:
            raise RuntimeError("catalog retry requires an acquisition service")
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
        result = self.acquisition.refresh(target_id, provider)
        with self.sessions() as session, session.begin():
            session.add(CatalogRetryAction(
                target_id=target_id,
                provider=provider,
                failed_run_id=run_id,
                retry_run_id=result.run_id,
                actor=decision.actor,
                reason=decision.reason,
            ))
        return result


def _result(
    run: CatalogRun,
    status: ProviderRunStatus,
    measurement_count: int,
    selected_source_id: str | None = None,
) -> CatalogRefreshResult:
    return CatalogRefreshResult(
        run.id, run.target_id, run.provider, status,
        run.candidate_count, measurement_count, selected_source_id,
    )


def _reconcile_iras(session: Session, run: CatalogRun) -> None:
    if run.provider in {"iras_psc", "iras_fsc"}:
        from .iras import reconcile_iras_target
        reconcile_iras_target(session, run.target_id)
