from __future__ import annotations

import html
import hashlib
import json
import math
import threading
import webbrowser
from collections import defaultdict
from typing import Callable
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .assignment_readiness import assignment_readiness_report
from .fitting_groups import fitting_group_report
from .models import RawCatalogRow
from .review_actions import (
    review_detection_decision,
    review_photometry_eligibility_decision,
    review_target_lifecycle_decision,
)
from .review_dashboard import review_dashboard_report
from .review_widget import build_review_sky_view, render_review_sky_html
from .service import IdentityService
from .system_expansion import (
    import_immediate_relatives,
    preview_immediate_relatives,
)


def create_review_app(
    session_factory: sessionmaker[Session], *, sample: str | None = None,
    identity_service_factory: Callable[[], IdentityService] | None = None,
):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
    except ImportError as error:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "review UI dependencies are missing; install with pip install -e '.[review]'"
        ) from error

    app = FastAPI(title="SDB review", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index(
        view: str = "actionable",
        priority: str = "",
        role: str = "",
        classification: str = "",
        provider: str = "",
        search: str = "",
    ):
        if sample is None:
            return _page(
                "SDB review",
                "<main><h1>SDB review</h1><p>Start the server with "
                "<code>--sample NAME</code> to populate the readiness queue.</p></main>",
            )
        report = review_dashboard_report(session_factory, sample=sample)
        return _queue_page(sample, report, _queue_filters(
            view=view,
            priority=priority,
            role=role,
            classification=classification,
            provider=provider,
            search=search,
        ))

    @app.get("/target/{sdbid}", response_class=HTMLResponse)
    def target(
        sdbid: str,
        view: str = "actionable",
        priority: str = "",
        role: str = "",
        classification: str = "",
        provider: str = "",
        search: str = "",
        position: int | None = None,
    ):
        try:
            filters = _queue_filters(
                view=view,
                priority=priority,
                role=role,
                classification=classification,
                provider=provider,
                search=search,
            )
            readiness = assignment_readiness_report(
                session_factory, target_reference=sdbid,
            )
            graph = fitting_group_report(
                session_factory, target_reference=sdbid,
            )
            raw_row_detections = _raw_row_detection_map(session_factory, graph)
            navigation = None
            if sample is not None:
                queue_report = review_dashboard_report(
                    session_factory, sample=sample,
                )
                navigation = _queue_navigation(
                    queue_report, sdbid, filters, position,
                )
            return _target_page(
                sdbid, readiness, graph, raw_row_detections, navigation,
            )
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/target/{sdbid}/sky", response_class=HTMLResponse)
    def target_sky(sdbid: str):
        try:
            view = build_review_sky_view(session_factory, sdbid)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return render_review_sky_html(view, embedded=True)

    @app.get("/api/readiness")
    def readiness_api():
        if sample is None:
            raise HTTPException(status_code=400, detail="server has no selected sample")
        return review_dashboard_report(session_factory, sample=sample)

    @app.get("/api/target/{sdbid}")
    def target_api(sdbid: str):
        try:
            return {
                "readiness": assignment_readiness_report(
                    session_factory, target_reference=sdbid,
                ),
                "fitting_graph": fitting_group_report(
                    session_factory, target_reference=sdbid,
                ),
            }
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/decision/preview")
    async def preview(payload: dict[str, object]):
        try:
            value = _decision_from_payload(
                session_factory, payload, apply=False,
            )
            return _with_human_summary(value, _decision_summary(value))
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/decision/apply")
    async def apply(payload: dict[str, object]):
        try:
            value = _decision_from_payload(
                session_factory, payload, apply=True,
            )
            return _with_human_summary(value, _decision_summary(value))
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/lifecycle/preview")
    async def lifecycle_preview(payload: dict[str, object]):
        try:
            value = _lifecycle_from_payload(
                session_factory, payload, apply=False,
            )
            return _with_human_summary(value, _lifecycle_summary(value))
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/lifecycle/apply")
    async def lifecycle_apply(payload: dict[str, object]):
        try:
            value = _lifecycle_from_payload(
                session_factory, payload, apply=True,
            )
            return _with_human_summary(value, _lifecycle_summary(value))
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/eligibility/preview")
    async def eligibility_preview(payload: dict[str, object]):
        try:
            value = _eligibility_from_payload(
                session_factory, payload, apply=False,
            )
            return _with_human_summary(value, _eligibility_summary(value))
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/eligibility/apply")
    async def eligibility_apply(payload: dict[str, object]):
        try:
            value = _eligibility_from_payload(
                session_factory, payload, apply=True,
            )
            return _with_human_summary(value, _eligibility_summary(value))
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/relatives/preview")
    async def relatives_preview(payload: dict[str, object]):
        try:
            return _relative_from_payload(
                session_factory,
                identity_service_factory,
                payload,
                apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/relatives/apply")
    async def relatives_apply(payload: dict[str, object]):
        try:
            return _relative_from_payload(
                session_factory,
                identity_service_factory,
                payload,
                apply=True,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


def serve_review_ui(
    session_factory: sessionmaker[Session],
    *,
    sample: str | None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    identity_service_factory: Callable[[], IdentityService] | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the review UI currently binds to localhost only")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "review UI dependencies are missing; install with pip install -e '.[review]'"
        ) from error
    app = create_review_app(
        session_factory,
        sample=sample,
        identity_service_factory=identity_service_factory,
    )
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


def _decision_from_payload(
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    return review_detection_decision(
        session_factory,
        detection_id=int(payload["detection_id"]),
        scope_target_reference=str(payload["scope_target"]),
        contributor_references=[
            str(value) for value in payload.get("contributors", [])
        ],
        include_composite_scope=bool(payload.get("include_composite_scope")),
        measurement_ids=[
            int(value) for value in payload.get("measurement_ids", [])
        ],
        target_role=(
            None if payload.get("target_role") in {None, ""}
            else str(payload["target_role"])
        ),
        target_state=(
            None if payload.get("target_state") in {None, ""}
            else str(payload["target_state"])
        ),
        apply=apply,
        actor=None if not apply else _optional_text(payload.get("actor")),
        reason=None if not apply else _optional_text(payload.get("reason")),
        expected_token=(
            None if not apply else str(payload.get("state_token", ""))
        ),
    )


def _lifecycle_from_payload(
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    return review_target_lifecycle_decision(
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


def _eligibility_from_payload(
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list):
        raise ValueError("changes must be a list")
    return review_photometry_eligibility_decision(
        session_factory,
        changes=[dict(value) for value in raw_changes],
        apply=apply,
        actor=None if not apply else _optional_text(payload.get("actor")),
        reason=None if not apply else _optional_text(payload.get("reason")),
        expected_token=(
            None if not apply else str(payload.get("state_token", ""))
        ),
    )


def _with_human_summary(
    value: dict[str, object], summary: dict[str, object],
) -> dict[str, object]:
    return {**value, "human_summary": summary}


def _decision_summary(value: dict[str, object]) -> dict[str, object]:
    measurements = {
        int(row["measurement_id"]): row for row in value["measurements"]
    }
    bands = [str(row["band"]) for row in value["measurements"]]
    changes = []
    for verb, key in (("Add", "add_assignments"), ("Remove", "remove_assignments")):
        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in value[key]:
            measurement = measurements.get(int(row["measurement_id"]))
            grouped[(str(row["role"]), str(row["sdbid"]))].append(
                str(row["measurement_id"]) if measurement is None
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
        str(row["band"]) for row in value["measurements"] if row.get("excluded")
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
        else ("Decision changes ready" if value["has_changes"] else "No assignment changes")
    )
    detection = value["detection"]
    return {
        "title": title,
        "facts": [
            f"Detection: {detection['provider']} {detection['source_id']}",
            f"Selected bands: {', '.join(bands)}",
            f"Scope target: {value['scope_target']['sdbid']}",
        ],
        "changes": changes or ["Current ownership already matches this decision."],
        "warnings": warnings,
    }


def _lifecycle_summary(value: dict[str, object]) -> dict[str, object]:
    reconciliation = value.get("assignment_reconciliation") or []
    changes = []
    if value["current"] != value["desired"]:
        changes.append(
            f"Change role from {value['current']['role']}/{value['current']['state']} "
            f"to {value['desired']['role']}/{value['desired']['state']}."
        )
    if reconciliation:
        changes.append(
            f"Convert {len(reconciliation)} existing composite-scope assignment"
            f"{'s' if len(reconciliation) != 1 else ''} to contributor ownership."
        )
    applied = value.get("applied") or {}
    title = (
        f"Applied role decision ({int(applied.get('lifecycle_actions', 0))} lifecycle action)"
        if value.get("mode") == "applied"
        else ("Role changes ready" if value["has_changes"] else "No role changes")
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
        f"Applied {int(applied.get('overrides_added', 0))} fit-eligibility override"
        f"{'s' if int(applied.get('overrides_added', 0)) != 1 else ''}"
        if value.get("mode") == "applied"
        else ("Fit-eligibility changes ready" if changed else "No eligibility changes")
    )
    return {
        "title": title,
        "facts": [
            "These controls affect fitting/export eligibility, not ownership.",
            f"Reviewed bands: {len(value['changes'])}",
        ],
        "changes": changes or ["The latest manual overrides already match."],
        "warnings": ([
            f"{len(unchanged)} selected setting"
            f"{'s are' if len(unchanged) != 1 else ' is'} already current."
        ] if unchanged else []),
    }


def _relative_from_payload(
    session_factory: sessionmaker[Session],
    identity_service_factory: Callable[[], IdentityService] | None,
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    target = str(payload["target"])
    preview = _relative_preview_payload(session_factory, target)
    if not apply:
        return preview
    if identity_service_factory is None:
        raise RuntimeError(
            "relative import is unavailable in offline review mode; restart without --offline"
        )
    expected_token = str(payload.get("state_token") or "")
    if not expected_token or expected_token != preview["state_token"]:
        raise RuntimeError(
            "SIMBAD relative state changed after preview; reload and preview again"
        )
    actor = _optional_text(payload.get("actor"))
    reason = _optional_text(payload.get("reason"))
    result = import_immediate_relatives(
        session_factory,
        target,
        identity_service=identity_service_factory(),
        actor=actor or "",
        reason=reason or "",
    ).as_dict()
    value = {
        **preview,
        **result,
        "mode": "applied",
        "has_changes": False,
    }
    return _with_human_summary(value, _relative_summary(value))


def _relative_preview_payload(
    session_factory: sessionmaker[Session], target_reference: str | int,
) -> dict[str, object]:
    rows = preview_immediate_relatives(session_factory, target_reference)
    token_rows = [{
        "relationship_id": row["relationship_id"],
        "action": row["action"],
        "matched_target_id": row["matched_target_id"],
        "component_label": row["component_label"],
        "suggested_role": row["suggested_role"],
        "suggested_state": row["suggested_state"],
    } for row in rows]
    token = hashlib.sha256(
        json.dumps(token_rows, sort_keys=True).encode("utf-8")
    ).hexdigest()
    counts = {
        action: sum(row["action"] == action for row in rows)
        for action in ("import", "already_imported", "context_only", "review_required")
    }
    value = {
        "mode": "preview",
        "target": str(target_reference),
        "state_token": token,
        "has_changes": bool(counts["import"] or counts["already_imported"]),
        "counts": counts,
        "relatives": rows,
    }
    return _with_human_summary(value, _relative_summary(value))


def _relative_summary(value: dict[str, object]) -> dict[str, object]:
    rows = value["relatives"]
    changes = []
    warnings = []
    for row in rows:
        label = str(row["main_id"])
        component = (
            "" if not row.get("component_label")
            else f" component {row['component_label']}"
        )
        if row["action"] == "import":
            changes.append(
                f"Import {label} as {row['suggested_role']}{component}."
            )
        elif row["action"] == "imported":
            changes.append(
                f"Imported {label} as {row['suggested_role']}{component}: "
                f"{row['matched_sdbid']}."
            )
        elif row["action"] == "already_imported":
            changes.append(
                f"Reconcile existing {row['matched_sdbid']} with {label}{component}."
            )
        elif row["action"] == "review_required":
            warnings.append(f"Review required: {label} — {row['reason']}")
        elif row["action"] == "context_only":
            warnings.append(f"Context only: {label} — {row['component_relevance']}")
        elif row["action"] == "failed":
            warnings.append(f"Import failed: {label} — {row.get('error', 'unknown error')}")
    if value.get("mode") == "applied":
        title = (
            f"Relative import finished: {int(value.get('imported', 0))} imported, "
            f"{int(value.get('already_imported', 0))} already present, "
            f"{int(value.get('failed', 0))} failed"
        )
    else:
        title = "SIMBAD-relative changes ready" if value["has_changes"] else "No relatives to import"
    return {
        "title": title,
        "facts": [
            f"Target: {value['target']}",
            "Only immediate stellar/substellar relatives are imported; expansion is not recursive.",
        ],
        "changes": changes or ["No immediate stellar relatives need importing or reconciliation."],
        "warnings": warnings,
    }


def _queue_filters(**values: str) -> dict[str, str]:
    return {
        key: str(value or "").strip()
        for key, value in values.items()
        if str(value or "").strip()
    }


def _filtered_queue_rows(
    report: dict[str, object], filters: dict[str, str],
) -> list[dict[str, object]]:
    rows = list(report["rows"])
    view = filters.get("view", "actionable")
    if view == "actionable":
        rows = [row for row in rows if row["priority"] != "none"]
    elif view == "clean":
        rows = [row for row in rows if row["priority"] == "none"]
    elif view != "all":
        rows = []
    for key in ("priority", "role", "classification"):
        if key in filters:
            rows = [row for row in rows if str(row[key]) == filters[key]]
    if "provider" in filters:
        rows = [
            row for row in rows
            if any(
                str(value["provider"]) == filters["provider"]
                for value in row["providers"]
            )
        ]
    if "search" in filters:
        needle = filters["search"].casefold()
        rows = [row for row in rows if needle in " ".join([
            str(row["sdbid"]),
            str(row["recommended_action"]),
            str(row["classification"]),
            *(str(value["provider"]) for value in row["providers"]),
            *(str(band) for value in row["providers"] for band in value["bands"]),
        ]).casefold()]
    return rows


def _queue_query(filters: dict[str, str], position: int | None = None) -> str:
    values: dict[str, object] = dict(filters)
    if position is not None:
        values["position"] = position
    encoded = urlencode(values)
    return "" if not encoded else f"?{encoded}"


def _queue_navigation(
    report: dict[str, object],
    sdbid: str,
    filters: dict[str, str],
    requested_position: int | None,
) -> dict[str, object]:
    rows = _filtered_queue_rows(report, filters)
    current_index = next(
        (index for index, row in enumerate(rows) if row["sdbid"] == sdbid),
        None,
    )
    current_present = current_index is not None
    if current_index is None:
        cursor = max(0, int(requested_position or 0))
        previous_index = min(cursor - 1, len(rows) - 1)
        next_index = cursor if cursor < len(rows) else None
    else:
        previous_index = current_index - 1
        next_index = current_index + 1 if current_index + 1 < len(rows) else None

    def target_url(index: int | None) -> str | None:
        if index is None or index < 0 or index >= len(rows):
            return None
        target = str(rows[index]["sdbid"])
        return f"/target/{quote(target)}{_queue_query(filters, index)}"

    display_position = (
        current_index + 1 if current_index is not None
        else min(max(int(requested_position or 0), 0) + 1, len(rows))
    )
    return {
        "filters": filters,
        "back_url": f"/{_queue_query(filters)}",
        "previous_url": target_url(previous_index),
        "next_url": target_url(next_index),
        "position": display_position,
        "count": len(rows),
        "current_present": current_present,
    }


def _select_options(
    values: list[str], selected: str, *, empty_label: str,
) -> str:
    options = [
        f"<option value=''{' selected' if not selected else ''}>{_e(empty_label)}</option>"
    ]
    options.extend(
        f"<option value='{_e(value)}'{' selected' if value == selected else ''}>"
        f"{_e(value.replace('_', ' '))}</option>"
        for value in values
    )
    return "".join(options)


def _queue_page(
    sample: str,
    report: dict[str, object],
    filters: dict[str, str],
) -> str:
    filtered_rows = _filtered_queue_rows(report, filters)
    rows = []
    for position, row in enumerate(filtered_rows):
        provider_text = "; ".join(
            f"{value['provider']}: {', '.join(value['bands'])}"
            for value in row["providers"]
        )
        target_query = _queue_query(filters, position)
        rows.append(
            f"<tr class='priority-{_e(row['priority'])}' data-classification='{_e(row['classification'])}'>"
            f"<td>{_e(row['priority'])}</td>"
            f"<td><a href='/target/{quote(str(row['sdbid']))}{_e(target_query)}'><code>{_e(row['sdbid'])}</code></a></td>"
            f"<td>{_e(str(row['classification']).replace('_', ' '))}</td>"
            f"<td>{_e(row['role'])}</td><td>{row['detection_count']} / {row['measurement_count']} bands</td>"
            f"<td>{row['unassigned_detection_count']} / {row['mixed_detection_count']}</td>"
            f"<td>{_e(provider_text)}</td>"
            f"<td>{_e(row['recommended_action'])}</td></tr>"
        )
    summary = report["summary"]
    all_rows = list(report["rows"])
    available_priorities = {str(row["priority"]) for row in all_rows}
    priorities = [
        value for value in ("highest", "high", "medium", "low")
        if value in available_priorities
    ] + sorted(available_priorities - {"highest", "high", "medium", "low"})
    roles = sorted({str(row["role"]) for row in all_rows})
    classifications = sorted({str(row["classification"]) for row in all_rows})
    providers = sorted({
        str(value["provider"]) for row in all_rows for value in row["providers"]
    })
    body = f"""
<main>
  <h1>SDB sample review</h1>
  <p class="muted">Sample <code>{_e(sample)}</code>. Current detections and accepted ownership are live from SQLite; open a target for detailed proposals.</p>
  <div class="summary">
    <span><strong>{summary['target_count']}</strong> sample targets</span>
    <span><strong>{summary['actionable_target_count']}</strong> actionable</span>
    <span><strong>{summary['clean_target_count']}</strong> clean/current</span>
    <span><strong>{summary['scope_blocker_target_count']}</strong> scope blockers</span>
    <span><strong>{summary['unassigned_detection_count']}</strong> unassigned detections</span>
    <span><strong>{summary['mixed_detection_count']}</strong> mixed-ownership detections</span>
  </div>
  <form class="queue-filters" method="get">
    <label>View <select name="view">{_select_options(['all', 'clean'], '' if filters.get('view', 'actionable') == 'actionable' else filters.get('view', ''), empty_label='actionable')}</select></label>
    <label>Priority <select name="priority">{_select_options(priorities, filters.get('priority', ''), empty_label='all priorities')}</select></label>
    <label>Role <select name="role">{_select_options(roles, filters.get('role', ''), empty_label='all roles')}</select></label>
    <label>Classification <select name="classification">{_select_options(classifications, filters.get('classification', ''), empty_label='all classifications')}</select></label>
    <label>Provider <select name="provider">{_select_options(providers, filters.get('provider', ''), empty_label='all providers')}</select></label>
    <label>Search <input name="search" value="{_e(filters.get('search', ''))}" placeholder="target, band, action"></label>
    <div class="filter-actions"><button type="submit">Apply filters</button><a href="/">Clear</a></div>
  </form>
  <p class="muted queue-count">Showing <strong>{len(filtered_rows)}</strong> of {len(all_rows)} sample targets.</p>
  <table><thead><tr><th>priority</th><th>target</th><th>classification</th><th>role</th><th>detections</th><th>unassigned / mixed</th><th>providers/bands</th><th>recommended action</th></tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="8" class="muted">No sample targets match these filters.</td></tr>'}</tbody></table>
</main>"""
    return _page(f"SDB review: {sample}", body)


def _target_page(
    sdbid: str,
    readiness: dict[str, object],
    graph: dict[str, object],
    raw_row_detections: dict[int, int],
    navigation: dict[str, object] | None = None,
) -> str:
    target = next(
        (row for row in graph["targets"] if row["sdbid"] == sdbid),
        None,
    )
    if target is None:
        raise KeyError(f"target is not present in its fitting graph: {sdbid}")
    targets = sorted(graph["targets"], key=lambda row: str(row["sdbid"]))
    detection_rows: dict[int, list[dict[str, object]]] = defaultdict(list)
    for measurement in graph["measurements"]:
        detection_rows[int(measurement["detection_id"])].append(measurement)
    cards = []
    for detection_id, measurements in sorted(
        detection_rows.items(),
        key=lambda item: (item[1][0]["provider"], item[1][0]["source_id"]),
    ):
        first = measurements[0]
        current_contributors = set.intersection(*(
            set(row["contributor_sdbids"]) for row in measurements
        ))
        all_current_contributors = set().union(*(
            set(row["contributor_sdbids"]) for row in measurements
        ))
        current_scopes = set.intersection(*(
            set(row["composite_scope_sdbids"]) for row in measurements
        ))
        all_current_scopes = set().union(*(
            set(row["composite_scope_sdbids"]) for row in measurements
        ))
        contributor_patterns = {
            tuple(sorted(row["contributor_sdbids"])) for row in measurements
        }
        scope_patterns = {
            tuple(sorted(row["composite_scope_sdbids"])) for row in measurements
        }
        mixed_assignments = len(contributor_patterns) > 1 or len(scope_patterns) > 1
        target_choices = "".join(
            f"<label><input type='checkbox' class='contributor' "
            f"value='{_e(row['sdbid'])}'"
            f"{' checked' if row['sdbid'] in current_contributors else ''}> "
            f"<code>{_e(row['sdbid'])}</code> ({_e(row['role'])})</label>"
            for row in targets
            if row["model_target"] or row["sdbid"] in all_current_contributors
        )
        default_scope = next(iter(sorted(current_scopes)), None)
        if default_scope is None:
            default_scope = (
                sdbid if sdbid in all_current_scopes
                else str(first["origin_sdbid"] or sdbid)
            )
        scope_choices = "".join(
            f"<option value='{_e(row['sdbid'])}'"
            f"{' selected' if row['sdbid'] == default_scope else ''}>"
            f"{_e(row['sdbid'])} ({_e(row['role'])})</option>"
            for row in targets
        )
        bands = "".join(
            f"<div class='band-row'><label><input type='checkbox' class='measurement' "
            f"value='{row['measurement_id']}' checked> "
            f"{_e(row['band'])}: {_display_number(row['value'])} ± "
            f"{_display_number(row['error'])} {_e(row['unit'])}</label>"
            f"<span class='eligibility-state {'excluded' if row['fit_excluded'] else 'included'}'>"
            f"{'excluded' if row['fit_excluded'] else 'included'} · "
            f"{_e(str(row['exclusion_basis']).replace('_', ' '))}</span>"
            f"<select class='eligibility' data-target='{_e(row['origin_sdbid'])}' "
            f"data-provider='{_e(row['provider'])}' data-band='{_e(row['band'])}' "
            f"aria-label='Fit eligibility for {_e(row['band'])}'>"
            f"<option value=''>Leave unchanged</option>"
            f"<option value='include'>Include in fit/export</option>"
            f"<option value='exclude'>Show but exclude from fit</option></select></div>"
            for row in measurements
        )
        cards.append(f"""
<section class="detection" data-detection="{detection_id}">
  <h3>{_e(first['provider'])} · {_e(first['source_id'])}</h3>
  <div class="bands">{bands}</div>
  {"<p class='warning'>Bands currently have different assignments. Their common assignments are selected below; preview carefully before applying.</p>" if mixed_assignments else ""}
  <h4>Contributors</h4><div class="choices">{target_choices}</div>
  <label>Composite scope target <select class="scope-target">{scope_choices}</select></label>
  <label><input type="checkbox" class="composite-scope"{' checked' if default_scope in current_scopes else ''}> retain selected target as composite scope</label>
  <div><button class="preview">Preview decision</button></div>
  <div><button class="preview-eligibility" type="button">Preview fit eligibility</button></div>
</section>""")
    readiness_text = (
        "No system-level blocker for this target."
        if not readiness["rows"]
        else _e(readiness["rows"][0]["recommended_action"])
    )
    if navigation is None:
        back_url = "/"
        navigation_html = ""
    else:
        back_url = str(navigation["back_url"])
        previous = (
            "<span class='nav-disabled'>← Previous</span>"
            if navigation["previous_url"] is None
            else f"<a rel='prev' href='{_e(navigation['previous_url'])}'>← Previous</a>"
        )
        following = (
            "<span class='nav-disabled'>Next →</span>"
            if navigation["next_url"] is None
            else f"<a rel='next' href='{_e(navigation['next_url'])}'>Next →</a>"
        )
        queue_state = (
            f"{navigation['position']} of {navigation['count']}"
            if navigation["current_present"]
            else f"resolved/filtered out · {navigation['count']} remain"
        )
        navigation_html = (
            f"<nav class='queue-navigation' aria-label='Readiness queue navigation'>"
            f"{previous}<span>{_e(queue_state)}</span>{following}</nav>"
        )
    body = f"""
<main class="live-workspace">
  <header class="live-header">
    <span><a href="{_e(back_url)}">← readiness queue</a></span>
    <strong><code>{_e(sdbid)}</code></strong>
    <span class="muted">{_e(target['role'])}/{_e(target['state'])} · {readiness_text}</span>
    {navigation_html}
    <button id="classify-target" class="{'needs-decision' if target['role'] == 'unspecified' else ''}" type="button">{'Decide target role' if target['role'] == 'unspecified' else 'Change target role'}</button>
  </header>
  <iframe id="sky-review" title="Sky and system review for {_e(sdbid)}" src="/target/{quote(sdbid)}/sky"></iframe>
  <aside id="assignment-drawer" class="assignment-drawer" hidden>
    <div class="drawer-header"><div><h2>Photometry assignment</h2><div id="selected-source" class="muted"></div></div><button id="close-drawer" type="button" aria-label="Close assignment editor">×</button></div>
    <p id="assignment-prompt" class="muted">Select a plotted catalog source with normalized photometry.</p>
    <section class="decision-meta"><label>Actor <input id="actor"></label><label>Reason <input id="reason"></label></section>
    <div id="detection-editors">{''.join(cards) or '<p>No current measurements.</p>'}</div>
    <section class="preview-panel"><h2>Decision preview</h2><div id="preview" class="change-summary muted">Choose assignments, then preview.</div><button id="apply" disabled>Apply audited decision</button></section>
    <section class="preview-panel"><h2>Fit eligibility preview</h2><p class="muted">Include/exclude is independent of component ownership. It applies to the measurement's origin target, provider, and band.</p><div id="eligibility-preview" class="change-summary muted">Choose a band setting, then preview.</div><button id="apply-eligibility" disabled>Apply eligibility overrides</button></section>
  </aside>
  <dialog id="lifecycle-dialog">
    <form method="dialog" class="dialog-header"><div><h2>Target modelling role</h2><code>{_e(sdbid)}</code></div><button value="cancel" aria-label="Close target role dialog">×</button></form>
    <p>This decision describes how the target participates in fitting; it does not assert whether the object is single or multiple.</p>
    <label class="role-choice"><input type="radio" name="lifecycle-role" value="physical"{' checked' if target['role'] != 'composite' else ''}> <strong>Physical / fitted model</strong><span>Fit one photospheric model for this target. Use this for an unresolved combined-light AB system when A and B are not separately modelled; WDS multiplicity remains recorded.</span></label>
    <label class="role-choice"><input type="radio" name="lifecycle-role" value="composite"{' checked' if target['role'] == 'composite' else ''}> <strong>Composite / measurement scope</strong><span>Do not fit this target itself. Its measurements must be assigned to separately imported physical contributors such as A and B.</span></label>
    <p id="lifecycle-warning" class="warning" hidden>A composite without imported physical contributors will remain unresolved for joint fitting. Choose physical if one combined-light model is the intended approximation.</p>
    <section class="decision-meta"><label>Actor <input id="lifecycle-actor"></label><label>Reason <input id="lifecycle-reason"></label></section>
    <div class="dialog-actions"><button id="preview-lifecycle" type="button">Preview role decision</button><button id="apply-lifecycle" type="button" disabled>Apply audited decision</button></div>
    <div id="lifecycle-preview" class="change-summary muted">Choose a role, then preview.</div>
  </dialog>
  <dialog id="relatives-dialog">
    <form method="dialog" class="dialog-header"><div><h2>Immediate SIMBAD relatives</h2><code>{_e(sdbid)}</code></div><button value="cancel" aria-label="Close relative import dialog">×</button></form>
    <p>Preview or import only immediate stellar/substellar parents and children. Contextual groups, planets, disks, and unknown object types are retained for review but are not imported; newly imported targets are not expanded recursively.</p>
    <section class="decision-meta"><label>Actor <input id="relatives-actor"></label><label>Reason <input id="relatives-reason"></label></section>
    <div class="dialog-actions"><button id="preview-relatives" type="button">Refresh preview</button><button id="apply-relatives" type="button" disabled>Import and reconcile stellar relatives</button></div>
    <div id="relatives-preview" class="change-summary muted">Open this dialog from Immediate SIMBAD relatives in the system column.</div>
  </dialog>
</main>
<script>window.SDB_TARGET={json.dumps(sdbid)};window.SDB_RAW_ROW_DETECTIONS={json.dumps(raw_row_detections, sort_keys=True)};</script>
<script>{_WORKSPACE_JS}</script>"""
    return _page(f"SDB review: {sdbid}", body, body_class="live-review")


def _page(title: str, body: str, *, body_class: str = "") -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_e(title)}</title>
<style>{_CSS}</style></head><body class="{_e(body_class)}">{body}</body></html>"""


def _raw_row_detection_map(
    session_factory: sessionmaker[Session], graph: dict[str, object],
) -> dict[int, int]:
    detection_ids = {
        int(row["detection_id"]) for row in graph["measurements"]
    }
    if not detection_ids:
        return {}
    with session_factory() as session:
        return {
            int(raw_row_id): int(detection_id)
            for raw_row_id, detection_id in session.execute(
                select(RawCatalogRow.id, RawCatalogRow.detection_id)
                .where(RawCatalogRow.detection_id.in_(detection_ids))
            )
        }


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _display_number(value: object) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(value)
    absolute = abs(number)
    if absolute == 0 or absolute >= 0.01:
        return f"{number:.2f}"
    decimals = min(10, max(3, math.ceil(-math.log10(absolute)) + 1))
    return f"{number:.{decimals}f}"


_CSS = """
body{font-family:system-ui,-apple-system,sans-serif;color:#172033;margin:0;background:#f6f8fb}main{padding:22px;max-width:1600px;margin:auto}a{color:#2357a6}code{font-family:ui-monospace,monospace}.muted{color:#64748b}.summary{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}.summary span,.decision-meta,.detection,.preview-panel{background:white;border:1px solid #dce3ec;border-radius:8px;padding:12px}table{border-collapse:collapse;width:100%;background:white}th,td{padding:8px;border-bottom:1px solid #e5eaf0;text-align:left;vertical-align:top}.priority-highest{background:#fff1e8}.priority-high{background:#fffbea}.queue-filters{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr)) minmax(180px,1.5fr) auto;gap:10px;align-items:end;background:white;border:1px solid #dce3ec;border-radius:8px;padding:12px}.queue-filters select,.queue-filters input{box-sizing:border-box;width:100%;min-height:34px}.filter-actions{display:flex;gap:10px;align-items:center;min-height:34px}.queue-count{margin:10px 0}.detection{display:none;margin:12px 0}.detection.active{display:block}.bands,.choices{display:grid;gap:7px;margin:8px 0 12px}.band-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 8px;padding-bottom:7px;border-bottom:1px solid #eef2f7}.band-row .eligibility-state{font-size:12px;align-self:center}.band-row .included{color:#28734a}.band-row .eligibility{grid-column:1/-1;width:100%}.decision-meta{display:grid;grid-template-columns:1fr;gap:10px;margin:12px 0}.decision-meta input{box-sizing:border-box;width:100%}label{display:block}.excluded{color:#a12828}.warning{background:#fff8df;border-left:4px solid #d99b16;padding:8px}button{padding:7px 12px}pre{white-space:pre-wrap;max-height:34vh;overflow:auto;font-size:12px}.change-summary{margin:10px 0}.change-summary h3{margin:0 0 8px;color:#172033}.change-summary ul{margin:7px 0;padding-left:20px}.change-summary .summary-warning{color:#92400e}.change-summary details{margin-top:10px}.change-summary details pre{background:#f1f5f9;border-radius:6px;padding:8px;color:#334155}.live-review{overflow:hidden;--drawer-width:min(420px,38vw)}.live-workspace{padding:0;max-width:none;height:100vh}.live-header{height:48px;box-sizing:border-box;display:flex;gap:18px;align-items:center;padding:8px 16px;background:#fff;border-bottom:1px solid #dce3ec;white-space:nowrap;overflow:hidden}.live-header .muted{overflow:hidden;text-overflow:ellipsis}.live-header button{margin-left:auto;white-space:nowrap}.queue-navigation{display:flex;gap:9px;align-items:center;margin-left:auto;font-size:13px}.queue-navigation .nav-disabled{color:#94a3b8}.live-header .queue-navigation+button{margin-left:0}.live-header button.needs-decision{background:#fff4db;border:1px solid #d99b16;border-radius:6px;font-weight:700}.live-workspace iframe{display:block;width:100%;height:calc(100vh - 48px);border:0;transition:width .18s ease}.drawer-open .live-workspace iframe{width:calc(100% - var(--drawer-width))}.assignment-drawer{position:fixed;z-index:20;right:0;top:48px;width:var(--drawer-width);height:calc(100vh - 48px);box-sizing:border-box;overflow:auto;background:#f6f8fb;border-left:1px solid #cbd5e1;box-shadow:-8px 0 20px rgba(15,23,42,.12);padding:14px}.drawer-header,.dialog-header{display:flex;align-items:start;justify-content:space-between;gap:12px}.drawer-header h2,.dialog-header h2{margin:0}.drawer-header button,.dialog-header button{border:0;background:transparent;font-size:28px;line-height:1;cursor:pointer}.preview-panel{margin-top:12px}dialog{width:min(700px,90vw);max-height:88vh;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:10px;padding:18px;color:#172033;background:#f8fafc;box-shadow:0 20px 60px rgba(15,23,42,.3)}dialog::backdrop{background:rgba(15,23,42,.45)}.role-choice{display:grid;grid-template-columns:24px 1fr;column-gap:6px;margin:12px 0;padding:12px;background:white;border:1px solid #dce3ec;border-radius:8px}.role-choice input{grid-row:1/3}.role-choice span{color:#64748b;margin-top:4px}.dialog-actions{display:flex;gap:10px}@media(max-width:1100px){.queue-filters{grid-template-columns:repeat(2,minmax(150px,1fr))}}@media(max-width:800px){.live-review{--drawer-width:min(360px,48vw)}}
"""


_WORKSPACE_JS = r"""
let currentPayload=null;
let currentPreview=null;
let currentEligibilityPayload=null;
let currentEligibilityPreview=null;
const drawer=document.getElementById('assignment-drawer');
function closeDrawer(){
  drawer.hidden=true;
  document.body.classList.remove('drawer-open');
  currentPayload=null;
  currentPreview=null;
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  document.getElementById('apply').disabled=true;
  document.getElementById('apply-eligibility').disabled=true;
  document.querySelectorAll('.detection').forEach(section=>section.classList.remove('active'));
}
function showDetection(point,detectionId){
  const section=document.querySelector(`.detection[data-detection="${detectionId}"]`);
  if(!section){closeDrawer();return;}
  document.querySelectorAll('.detection').forEach(value=>value.classList.toggle('active',value===section));
  document.getElementById('selected-source').textContent=`${point.provider} ${point.source_id} · ${Number(point.separation_arcsec).toFixed(2)} arcsec`;
  document.getElementById('assignment-prompt').textContent='Review the selected catalog detection. All bands are selected by default.';
  document.getElementById('preview').textContent='Choose assignments, then preview.';
  document.getElementById('eligibility-preview').textContent='Choose a band setting, then preview.';
  document.getElementById('apply').disabled=true;
  document.getElementById('apply-eligibility').disabled=true;
  currentPayload=null;
  currentPreview=null;
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  drawer.hidden=false;
  document.body.classList.add('drawer-open');
}
window.addEventListener('message',event=>{
  if(event.origin!==window.location.origin) return;
  if(event.data?.type==='sdb-review-relatives'){
    openRelativesDialog();
    return;
  }
  if(event.data?.type!=='sdb-review-selection') return;
  const point=event.data.point;
  if(!point){closeDrawer();return;}
  const detectionId=point.raw_row_id==null?null:window.SDB_RAW_ROW_DETECTIONS[String(point.raw_row_id)];
  if(detectionId==null){closeDrawer();return;}
  showDetection(point,detectionId);
});
document.getElementById('close-drawer').addEventListener('click',closeDrawer);
function payloadFor(section){
  return {
    detection_id:Number(section.dataset.detection),
    scope_target:section.querySelector('.scope-target').value,
    contributors:[...section.querySelectorAll('.contributor:checked')].map(x=>x.value),
    include_composite_scope:section.querySelector('.composite-scope').checked,
    measurement_ids:[...section.querySelectorAll('.measurement:checked')].map(x=>Number(x.value)),
    target_role:'',
    target_state:'',
  };
}
function eligibilityPayloadFor(section){
  const changes=[...section.querySelectorAll('.eligibility')]
    .filter(value=>value.value)
    .map(value=>({
      target:value.dataset.target,
      provider:value.dataset.provider,
      band:value.dataset.band,
      excluded:value.value==='exclude',
    }));
  return {changes};
}
async function request(url,payload){
  const response=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});
  const value=await response.json();
  if(!response.ok) throw new Error(value.detail||response.statusText);
  return value;
}
function renderHumanSummary(element,value){
  const summary=value?.human_summary;
  if(!summary){element.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);return;}
  element.classList.remove('muted');
  element.replaceChildren();
  const heading=document.createElement('h3');
  heading.textContent=summary.title;
  element.appendChild(heading);
  for(const [name,rows,className] of [
    ['Context',summary.facts||[],''],
    ['Changes',summary.changes||[],''],
    ['Warnings',summary.warnings||[],'summary-warning'],
  ]){
    if(!rows.length)continue;
    const label=document.createElement('strong');
    label.textContent=name;
    if(className)label.className=className;
    element.appendChild(label);
    const list=document.createElement('ul');
    if(className)list.className=className;
    for(const row of rows){const item=document.createElement('li');item.textContent=row;list.appendChild(item);}
    element.appendChild(list);
  }
  const details=document.createElement('details');
  const detailsLabel=document.createElement('summary');
  detailsLabel.textContent='Technical details';
  const raw=document.createElement('pre');
  raw.textContent=JSON.stringify(value,null,2);
  details.append(detailsLabel,raw);
  element.appendChild(details);
}
function renderRequestError(element,error){element.classList.add('muted');element.textContent=error.message;}
document.querySelectorAll('.preview').forEach(button=>button.addEventListener('click',async()=>{
  const section=button.closest('.detection');
  currentPayload=payloadFor(section);
  try{
    currentPreview=await request('/api/decision/preview',currentPayload);
    renderHumanSummary(document.getElementById('preview'),currentPreview);
    document.getElementById('apply').disabled=!currentPreview.has_changes;
  }catch(error){renderRequestError(document.getElementById('preview'),error);}
}));
document.querySelectorAll('.eligibility').forEach(select=>select.addEventListener('change',()=>{
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  document.getElementById('eligibility-preview').textContent='Eligibility changed; preview again.';
  document.getElementById('apply-eligibility').disabled=true;
}));
document.querySelectorAll('.preview-eligibility').forEach(button=>button.addEventListener('click',async()=>{
  const section=button.closest('.detection');
  currentEligibilityPayload=eligibilityPayloadFor(section);
  if(!currentEligibilityPayload.changes.length){
    currentEligibilityPreview=null;
    document.getElementById('eligibility-preview').textContent='Choose Include or Exclude for at least one band.';
    document.getElementById('apply-eligibility').disabled=true;
    return;
  }
  try{
    currentEligibilityPreview=await request('/api/eligibility/preview',currentEligibilityPayload);
    renderHumanSummary(document.getElementById('eligibility-preview'),currentEligibilityPreview);
    document.getElementById('apply-eligibility').disabled=!currentEligibilityPreview.has_changes;
  }catch(error){renderRequestError(document.getElementById('eligibility-preview'),error);}
}));
document.getElementById('apply').addEventListener('click',async()=>{
  if(!currentPayload||!currentPreview) return;
  const actor=document.getElementById('actor').value;
  const reason=document.getElementById('reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  const payload={...currentPayload,actor,reason,state_token:currentPreview.state_token};
  if(!confirm('Apply the displayed lifecycle and assignment changes?')) return;
  try{
    const value=await request('/api/decision/apply',payload);
    renderHumanSummary(document.getElementById('preview'),value);
    document.getElementById('apply').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('preview'),error);}
});
document.getElementById('apply-eligibility').addEventListener('click',async()=>{
  if(!currentEligibilityPayload||!currentEligibilityPreview)return;
  const actor=document.getElementById('actor').value;
  const reason=document.getElementById('reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  if(!confirm('Apply the displayed fit-eligibility overrides?'))return;
  const payload={...currentEligibilityPayload,actor,reason,state_token:currentEligibilityPreview.state_token};
  try{
    const value=await request('/api/eligibility/apply',payload);
    renderHumanSummary(document.getElementById('eligibility-preview'),value);
    document.getElementById('apply-eligibility').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('eligibility-preview'),error);}
});
const lifecycleDialog=document.getElementById('lifecycle-dialog');
let lifecyclePreview=null;
function selectedLifecycleRole(){return document.querySelector('input[name="lifecycle-role"]:checked').value;}
function updateLifecycleWarning(){document.getElementById('lifecycle-warning').hidden=selectedLifecycleRole()!=='composite';}
document.querySelectorAll('input[name="lifecycle-role"]').forEach(input=>input.addEventListener('change',()=>{
  lifecyclePreview=null;
  document.getElementById('apply-lifecycle').disabled=true;
  document.getElementById('lifecycle-preview').textContent='Role changed; preview again.';
  updateLifecycleWarning();
}));
document.getElementById('classify-target').addEventListener('click',()=>{
  updateLifecycleWarning();
  lifecycleDialog.showModal();
});
document.getElementById('preview-lifecycle').addEventListener('click',async()=>{
  const role=selectedLifecycleRole();
  const payload={target:window.SDB_TARGET,role,state:role==='composite'?'system_only':'active'};
  try{
    lifecyclePreview=await request('/api/lifecycle/preview',payload);
    renderHumanSummary(document.getElementById('lifecycle-preview'),lifecyclePreview);
    document.getElementById('apply-lifecycle').disabled=!lifecyclePreview.has_changes;
  }catch(error){renderRequestError(document.getElementById('lifecycle-preview'),error);}
});
document.getElementById('apply-lifecycle').addEventListener('click',async()=>{
  if(!lifecyclePreview)return;
  const actor=document.getElementById('lifecycle-actor').value;
  const reason=document.getElementById('lifecycle-reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  const role=selectedLifecycleRole();
  const payload={target:window.SDB_TARGET,role,state:role==='composite'?'system_only':'active',actor,reason,state_token:lifecyclePreview.state_token};
  if(!confirm('Apply the displayed target modelling role?'))return;
  try{
    const value=await request('/api/lifecycle/apply',payload);
    renderHumanSummary(document.getElementById('lifecycle-preview'),value);
    document.getElementById('apply-lifecycle').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('lifecycle-preview'),error);}
});
const relativesDialog=document.getElementById('relatives-dialog');
let relativesPreview=null;
async function refreshRelativesPreview(){
  const element=document.getElementById('relatives-preview');
  element.classList.add('muted');
  element.textContent='Loading current SIMBAD relatives…';
  document.getElementById('apply-relatives').disabled=true;
  try{
    relativesPreview=await request('/api/relatives/preview',{target:window.SDB_TARGET});
    renderHumanSummary(element,relativesPreview);
    document.getElementById('apply-relatives').disabled=!relativesPreview.has_changes;
  }catch(error){relativesPreview=null;renderRequestError(element,error);}
}
function openRelativesDialog(){
  if(!relativesDialog.open)relativesDialog.showModal();
  refreshRelativesPreview();
}
document.getElementById('preview-relatives').addEventListener('click',refreshRelativesPreview);
document.getElementById('apply-relatives').addEventListener('click',async()=>{
  if(!relativesPreview)return;
  const actor=document.getElementById('relatives-actor').value;
  const reason=document.getElementById('relatives-reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  if(!confirm('Import and reconcile the displayed immediate stellar relatives?'))return;
  const button=document.getElementById('apply-relatives');
  button.disabled=true;
  button.textContent='Importing…';
  try{
    const value=await request('/api/relatives/apply',{target:window.SDB_TARGET,actor,reason,state_token:relativesPreview.state_token});
    renderHumanSummary(document.getElementById('relatives-preview'),value);
    setTimeout(()=>location.reload(),1000);
  }catch(error){renderRequestError(document.getElementById('relatives-preview'),error);button.disabled=false;}
  finally{button.textContent='Import and reconcile stellar relatives';}
});
"""
