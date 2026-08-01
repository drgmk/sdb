"""Transport-independent commands used by review clients."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..catalogs.decisions import CatalogDecisionService
from ..models.catalogs import CatalogResultDecision, CatalogRun, RawCatalogRow
from ..models.identity import Target
from .actions import (
    review_catalog_target_association_decision,
    review_detection_decision,
    review_photometry_eligibility_decision,
    review_target_lifecycle_decision,
)
from ..vocabulary import PROVIDER_FAILURE_STATUSES


def review_detection_command(
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    value = review_detection_decision(
        session_factory,
        detection_id=int(payload["detection_id"]),
        scope_target_reference=str(payload["scope_target"]),
        contributor_references=[
            str(item) for item in payload.get("contributors", [])
        ],
        include_composite_scope=bool(payload.get("include_composite_scope")),
        measurement_ids=[
            int(item) for item in payload.get("measurement_ids", [])
        ],
        target_role=_optional_choice(payload.get("target_role")),
        target_state=_optional_choice(payload.get("target_state")),
        apply=apply,
        actor=None if not apply else _optional_text(payload.get("actor")),
        reason=None if not apply else _optional_text(payload.get("reason")),
        expected_token=(
            None if not apply else str(payload.get("state_token", ""))
        ),
    )
    return _with_human_summary(value, _decision_summary(value))


def review_lifecycle_command(
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    value = review_target_lifecycle_decision(
        session_factory,
        target_reference=str(payload["target"]),
        role=str(payload["role"]),
        state=str(payload["state"]),
        apply=apply,
        actor=None if not apply else _optional_text(payload.get("actor")),
        reason=None if not apply else _optional_text(payload.get("reason")),
        expected_token=(
            None if not apply else str(payload.get("state_token", ""))
        ),
    )
    return _with_human_summary(value, _lifecycle_summary(value))


def review_eligibility_command(
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list):
        raise ValueError("changes must be a list")
    value = review_photometry_eligibility_decision(
        session_factory,
        changes=[dict(item) for item in raw_changes],
        apply=apply,
        actor=None if not apply else _optional_text(payload.get("actor")),
        reason=None if not apply else _optional_text(payload.get("reason")),
        expected_token=(
            None if not apply else str(payload.get("state_token", ""))
        ),
    )
    return _with_human_summary(value, _eligibility_summary(value))


def review_catalog_association_command(
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    value = review_catalog_target_association_decision(
        session_factory,
        target_reference=str(payload["target"]),
        detection_id=int(payload["detection_id"]),
        action=str(payload["action"]),
        reviewed_raw_row_id=int(payload["raw_row_id"]),
        apply=apply,
        actor=None if not apply else _optional_text(payload.get("actor")),
        reason=None if not apply else _optional_text(payload.get("reason")),
        expected_token=(
            None if not apply else str(payload.get("state_token", ""))
        ),
    )
    return _with_human_summary(value, _catalog_association_summary(value))


def review_provider_result_command(
    session_factory: sessionmaker[Session],
    catalog_service_factory: Callable[[str, str], object] | None,
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    action = str(payload["action"])
    if action not in {"accept_candidate", "reviewed_no_match", "retry"}:
        raise ValueError(f"unknown provider-result action: {action}")
    run_id = int(payload["run_id"])
    raw_row_id = (
        None if payload.get("raw_row_id") is None
        else int(payload["raw_row_id"])
    )
    with session_factory() as session:
        run = session.get(CatalogRun, run_id)
        if run is None:
            raise KeyError(f"catalog run not found: {run_id}")
        target = session.get(Target, run.target_id)
        if target is None:
            raise KeyError(f"target not found for catalog run: {run_id}")
        raw = (
            None
            if raw_row_id is None
            else session.get(RawCatalogRow, raw_row_id)
        )
        latest_decision = session.scalar(
            select(CatalogResultDecision)
            .where(CatalogResultDecision.reviewed_run_id == run.id)
            .order_by(CatalogResultDecision.id.desc())
            .limit(1)
        )
        if action == "accept_candidate":
            if raw is None or raw.run_id != run.id:
                raise ValueError("selected catalog candidate is not from this run")
            if run.status != "ambiguous" or not run.is_current:
                raise ValueError(
                    "candidate acceptance requires a current ambiguous run"
                )
        elif action == "reviewed_no_match":
            if run.status != "ambiguous" or not run.is_current:
                raise ValueError(
                    "reviewed no-match requires a current ambiguous run"
                )
        else:
            if run.status not in PROVIDER_FAILURE_STATUSES:
                raise ValueError("retry requires a failed catalog run")
            latest_id = session.scalar(
                select(CatalogRun.id)
                .where(
                    CatalogRun.target_id == run.target_id,
                    CatalogRun.provider == run.provider,
                )
                .order_by(CatalogRun.id.desc())
                .limit(1)
            )
            if latest_id != run.id:
                raise ValueError("catalog failure has already been superseded")
        token_state = {
            "action": action,
            "run_id": run.id,
            "run_status": run.status,
            "run_is_current": run.is_current,
            "raw_row_id": None if raw is None else raw.id,
            "source_id": None if raw is None else raw.source_id,
            "decision_id": (
                None if latest_decision is None else latest_decision.id
            ),
            "decision_action": (
                None if latest_decision is None else latest_decision.action
            ),
            "decision_raw_row_id": (
                None
                if latest_decision is None
                else latest_decision.reviewed_raw_row_id
            ),
        }
        state_token = hashlib.sha256(
            json.dumps(token_state, sort_keys=True).encode("utf-8")
        ).hexdigest()
        source_id = None if raw is None else raw.source_id
        has_changes = not (
            latest_decision is not None
            and (
                (
                    action == "accept_candidate"
                    and latest_decision.action == "accept_detection"
                    and latest_decision.reviewed_raw_row_id == raw_row_id
                )
                or (
                    action == "reviewed_no_match"
                    and latest_decision.action == "reviewed_no_match"
                )
            )
        )
        base = {
            "action": action,
            "has_changes": has_changes,
            "state_token": state_token,
            "suggested_reason": {
                "accept_candidate": (
                    f"Selected {run.provider} source {source_id} for "
                    f"{target.sdbid}"
                ),
                "reviewed_no_match": (
                    f"Reviewed {run.provider} candidates for {target.sdbid}; "
                    "none is the target"
                ),
                "retry": (
                    f"Retried failed {run.provider} result for {target.sdbid}"
                ),
            }[action],
            "target": {"id": target.id, "sdbid": target.sdbid},
            "run": {
                "id": run.id,
                "provider": run.provider,
                "status": run.status,
                "error": run.error,
            },
            "candidate": (
                None
                if raw is None
                else {
                    "raw_row_id": raw.id,
                    "source_id": raw.source_id,
                    "separation_arcsec": raw.separation_arcsec,
                    "score": raw.score,
                }
            ),
        }
        provider = run.provider

    if not apply:
        if (
            action in {"accept_candidate", "retry"}
            and catalog_service_factory is None
        ):
            raise RuntimeError(
                f"{action.replace('_', ' ')} is unavailable in this review server"
            )
        if action in {"accept_candidate", "retry"}:
            catalog_service_factory(provider, action)
        value = {**base, "mode": "preview"}
        return _with_human_summary(value, _provider_result_summary(value))

    expected_token = str(payload.get("state_token", ""))
    if expected_token != state_token:
        raise ValueError("provider result changed after preview; preview again")
    actor = _optional_text(payload.get("actor"))
    reason = _optional_text(payload.get("reason"))
    if action == "reviewed_no_match":
        result = CatalogDecisionService(session_factory).reviewed_no_match(
            run_id,
            actor=actor,
            reason=reason,
        )
    else:
        if catalog_service_factory is None:
            raise RuntimeError(
                f"{action.replace('_', ' ')} is unavailable in this review server"
            )
        service = catalog_service_factory(provider, action)
        if action == "accept_candidate":
            result = service.accept_candidate(
                raw_row_id,
                actor=actor,
                reason=reason,
            )
        else:
            result = service.retry_failed_run(
                run_id,
                actor=actor,
                reason=reason,
            )
    value = {**base, "mode": "applied", "applied": result.__dict__}
    return _with_human_summary(value, _provider_result_summary(value))


def _optional_choice(value: object) -> str | None:
    return None if value in {None, ""} else str(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _with_human_summary(
    value: dict[str, object],
    summary: dict[str, object],
) -> dict[str, object]:
    return {**value, "human_summary": summary}


def _provider_result_summary(value: dict[str, object]) -> dict[str, object]:
    action = str(value["action"])
    run = value["run"]
    target = value["target"]
    if not isinstance(run, dict) or not isinstance(target, dict):
        raise TypeError("provider result command returned invalid summary data")
    provider = str(run["provider"])
    candidate = value.get("candidate")
    if action == "accept_candidate":
        if not isinstance(candidate, dict):
            raise TypeError("candidate acceptance has no selected candidate")
        change = (
            f"Accept {provider} source {candidate['source_id']} as the catalog "
            "match."
        )
    elif action == "reviewed_no_match":
        change = (
            f"Record that none of the reviewed {provider} candidates matches."
        )
    else:
        change = f"Retry the failed {provider} provider request."
    applied = value.get("applied") or {}
    title = (
        f"Provider action applied: {applied.get('status', action)}"
        if value["mode"] == "applied"
        else {
            "accept_candidate": "Catalog candidate ready to accept",
            "reviewed_no_match": "Reviewed no-match ready to record",
            "retry": "Provider retry ready",
        }[action]
    )
    warnings = []
    if action == "retry":
        warnings.append(
            "Retry performs a provider request and records its new versioned "
            "result."
        )
    return {
        "title": title,
        "facts": [
            f"Target: {target['sdbid']}",
            f"Current result: {provider} — {run['status']}",
        ],
        "changes": [change],
        "warnings": warnings,
    }


def _catalog_association_summary(
    value: dict[str, object],
) -> dict[str, object]:
    detection = value["detection"]
    target = value["target"]
    if not isinstance(detection, dict) or not isinstance(target, dict):
        raise TypeError("catalog association returned invalid summary data")
    action = str(value["action"])
    verb = "Accept" if action == "accept" else "Reject"
    source = detection.get("source_display_name") or detection["source_id"]
    change = (
        f"{verb} {detection['provider']} source {source} "
        f"{'as' if action == 'accept' else 'for'} {target['sdbid']}."
    )
    if value["mode"] == "applied":
        applied = value.get("applied") or {}
        added = int(applied.get("actions_added", 0))
        title = (
            "Catalog source association applied"
            if added
            else "Catalog source association already current"
        )
    else:
        title = (
            "Catalog source association ready"
            if value["has_changes"]
            else "Catalog source association already current"
        )
    return {
        "title": title,
        "facts": [
            f"Separation: {float(detection['separation_arcsec']):.2f} arcsec",
            "The original provider query and raw result remain unchanged.",
            "Measurement contributor/composite assignment is a separate decision.",
        ],
        "changes": (
            [change]
            if value["has_changes"]
            else [f"The latest manual decision is already {action}."]
        ),
        "warnings": [],
    }


def _decision_summary(value: dict[str, object]) -> dict[str, object]:
    measurement_rows = value["measurements"]
    if not isinstance(measurement_rows, list):
        raise TypeError("detection decision returned invalid measurements")
    measurements = {
        int(row["measurement_id"]): row for row in measurement_rows
    }
    bands = [str(row["band"]) for row in measurement_rows]
    changes = []
    for verb, key in (
        ("Add", "add_assignments"),
        ("Remove", "remove_assignments"),
    ):
        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in value[key]:
            measurement = measurements.get(int(row["measurement_id"]))
            grouped[(str(row["role"]), str(row["sdbid"]))].append(
                str(row["measurement_id"])
                if measurement is None
                else str(measurement["band"])
            )
        for (role, sdbid), assigned_bands in sorted(grouped.items()):
            changes.append(
                f"{verb} {role.replace('_', ' ')}: {sdbid} — "
                f"{', '.join(sorted(assigned_bands))}"
            )
    lifecycle = value.get("lifecycle_change")
    if lifecycle:
        changes.append(
            f"Change target role: {lifecycle['sdbid']} from "
            f"{lifecycle['from_role']}/{lifecycle['from_state']} to "
            f"{lifecycle['to_role']}/{lifecycle['to_state']}"
        )
    excluded = [
        str(row["band"]) for row in measurement_rows if row.get("excluded")
    ]
    warnings = []
    if excluded:
        warnings.append(
            "Provider exclusions remain unchanged for: " + ", ".join(excluded)
        )
    applied = value.get("applied") or {}
    title = (
        f"Applied {int(applied.get('assignments_added', 0))} additions and "
        f"{int(applied.get('assignments_removed', 0))} removals"
        if value.get("mode") == "applied"
        else (
            "Decision changes ready"
            if value["has_changes"]
            else "No assignment changes"
        )
    )
    detection = value["detection"]
    scope_target = value["scope_target"]
    return {
        "title": title,
        "facts": [
            f"Detection: {detection['provider']} "
            f"{detection.get('source_display_name') or detection['source_id']}",
            f"Selected bands: {', '.join(bands)}",
            f"Scope target: {scope_target['sdbid']}",
        ],
        "changes": changes or [
            "Current ownership already matches this decision."
        ],
        "warnings": warnings,
    }


def _lifecycle_summary(value: dict[str, object]) -> dict[str, object]:
    reconciliation = value.get("assignment_reconciliation") or []
    changes = []
    if value["current"] != value["desired"]:
        changes.append(
            f"Change role from {value['current']['role']}/"
            f"{value['current']['state']} to {value['desired']['role']}/"
            f"{value['desired']['state']}."
        )
    if reconciliation:
        changes.append(
            f"Convert {len(reconciliation)} existing composite-scope assignment"
            f"{'s' if len(reconciliation) != 1 else ''} to contributor ownership."
        )
    applied = value.get("applied") or {}
    title = (
        f"Applied role decision "
        f"({int(applied.get('lifecycle_actions', 0))} lifecycle action)"
        if value.get("mode") == "applied"
        else (
            "Role changes ready" if value["has_changes"] else "No role changes"
        )
    )
    return {
        "title": title,
        "facts": [
            f"Target: {value['target']['sdbid']}",
            str(value["interpretation"]["summary"]),
        ],
        "changes": changes or ["The target already has this modelling role."],
        "warnings": [str(value["interpretation"]["multiplicity"])],
    }


def _eligibility_summary(value: dict[str, object]) -> dict[str, object]:
    changed = [row for row in value["changes"] if row["has_change"]]
    unchanged = [row for row in value["changes"] if not row["has_change"]]
    changes = [
        f"{'Exclude' if row['desired_excluded'] else 'Include'} "
        f"{row['provider']} {row['band']} for {row['sdbid']}."
        for row in changed
    ]
    applied = value.get("applied") or {}
    title = (
        f"Applied {int(applied.get('actions_added', 0))} fit include/exclude "
        f"change{'s' if int(applied.get('actions_added', 0)) != 1 else ''}"
        if value.get("mode") == "applied"
        else (
            "Fit include/exclude changes ready"
            if changed
            else "No fit include/exclude changes"
        )
    )
    return {
        "title": title,
        "facts": [
            "These controls affect fitting/export eligibility, not ownership.",
            f"Reviewed bands: {len(value['changes'])}",
        ],
        "changes": changes or [
            "The effective measurement settings already match."
        ],
        "warnings": (
            [
                f"{len(unchanged)} selected setting"
                f"{'s are' if len(unchanged) != 1 else ' is'} already current."
            ]
            if unchanged
            else []
        ),
    }
