from __future__ import annotations

import html
import hashlib
import json
import math
import os
import threading
import webbrowser
from collections import defaultdict
from typing import Callable
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .assignment_readiness import assignment_readiness_report
from .decisions import DecisionContext
from .fitting_groups import fitting_group_report
from .hierarchy import HierarchyService
from .models import CatalogResultDecision, CatalogRun, RawCatalogRow, Target
from .review_actions import (
    review_catalog_target_association_decision,
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
from .target_import import (
    TargetImportService,
    search_nearby_simbad,
)
from .vocabulary import PROVIDER_FAILURE_STATUSES


def create_review_app(
    session_factory: sessionmaker[Session], *, sample: str | None = None,
    identity_service_factory: Callable[[], IdentityService] | None = None,
    catalog_service_factory: Callable[[str, str], object] | None = None,
    catalog_coverage_providers: tuple[str, ...] | None = None,
    catalog_update_factory: Callable[[], object] | None = None,
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
            system_context = HierarchyService(session_factory).system_context(
                sdbid,
                catalog_providers=catalog_coverage_providers,
            )
            simbad_main_ids = dict(
                system_context.get("simbad_main_id_by_target", {})
            )
            for relative in system_context.get("simbad_relative_preview", []):
                if (
                    relative.get("action") != "context_only"
                    and relative.get("matched_sdbid")
                    and relative.get("main_id")
                ):
                    simbad_main_ids.setdefault(
                        str(relative["matched_sdbid"]),
                        str(relative["main_id"]),
                    )
            raw_row_detections = _raw_row_detection_map(session_factory, graph)
            navigation = None
            display_name = None
            if sample is not None:
                queue_report = review_dashboard_report(
                    session_factory, sample=sample,
                )
                display_name = next((
                    str(row["display_name"])
                    for row in queue_report["rows"]
                    if row["sdbid"] == sdbid and row.get("display_name")
                ), None)
                navigation = _queue_navigation(
                    queue_report, sdbid, filters, position,
                )
            display_name = display_name or simbad_main_ids.get(sdbid)
            return _target_page(
                sdbid, readiness, graph, raw_row_detections, navigation,
                display_name, simbad_main_ids,
                catalog_coverage=list(
                    system_context.get("catalog_coverage_by_target", [])
                ),
                catalog_update_available=catalog_update_factory is not None,
                nearby_import_available=(
                    identity_service_factory is not None
                    and catalog_update_factory is not None
                ),
                target_position=dict(system_context["target"]),
            )
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/target/{sdbid}/sky", response_class=HTMLResponse)
    def target_sky(sdbid: str, radius: float | None = None):
        try:
            view = build_review_sky_view(
                session_factory,
                sdbid,
                radius_arcsec=radius,
            )
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

    @app.post("/api/provider-result/preview")
    async def provider_result_preview(payload: dict[str, object]):
        try:
            value = _provider_result_from_payload(
                session_factory,
                catalog_service_factory,
                payload,
                apply=False,
            )
            return _with_human_summary(
                value, _provider_result_summary(value),
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/provider-result/apply")
    async def provider_result_apply(payload: dict[str, object]):
        try:
            value = _provider_result_from_payload(
                session_factory,
                catalog_service_factory,
                payload,
                apply=True,
            )
            return _with_human_summary(
                value, _provider_result_summary(value),
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/catalog-association/preview")
    async def catalog_association_preview(payload: dict[str, object]):
        try:
            value = _catalog_association_from_payload(
                session_factory, payload, apply=False,
            )
            return _with_human_summary(
                value, _catalog_association_summary(value),
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/catalog-association/apply")
    async def catalog_association_apply(payload: dict[str, object]):
        try:
            value = _catalog_association_from_payload(
                session_factory, payload, apply=True,
            )
            return _with_human_summary(
                value, _catalog_association_summary(value),
            )
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

    @app.post("/api/catalog-coverage/preview")
    async def catalog_coverage_preview(payload: dict[str, object]):
        try:
            return _catalog_coverage_from_payload(
                session_factory,
                catalog_coverage_providers,
                catalog_update_factory,
                catalog_service_factory,
                payload,
                apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/catalog-coverage/apply")
    async def catalog_coverage_apply(payload: dict[str, object]):
        try:
            return _catalog_coverage_from_payload(
                session_factory,
                catalog_coverage_providers,
                catalog_update_factory,
                catalog_service_factory,
                payload,
                apply=True,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/nearby-import/search")
    async def nearby_import_search(payload: dict[str, object]):
        try:
            return _nearby_import_search_payload(
                session_factory,
                identity_service_factory,
                payload,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/nearby-import/apply")
    async def nearby_import_apply(payload: dict[str, object]):
        try:
            return _nearby_import_from_payload(
                session_factory,
                identity_service_factory,
                catalog_update_factory,
                catalog_coverage_providers,
                payload,
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
    catalog_service_factory: Callable[[str, str], object] | None = None,
    catalog_coverage_providers: tuple[str, ...] | None = None,
    catalog_update_factory: Callable[[], object] | None = None,
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
        catalog_service_factory=catalog_service_factory,
        catalog_coverage_providers=catalog_coverage_providers,
        catalog_update_factory=catalog_update_factory,
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


def _catalog_association_from_payload(
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    return review_catalog_target_association_decision(
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


def _provider_result_from_payload(
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
        raw = None if raw_row_id is None else session.get(
            RawCatalogRow, raw_row_id,
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
                raise ValueError("candidate acceptance requires a current ambiguous run")
        elif action == "reviewed_no_match":
            if run.status != "ambiguous" or not run.is_current:
                raise ValueError("reviewed no-match requires a current ambiguous run")
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
                    f"Selected {run.provider} source {source_id} for {target.sdbid}"
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
                None if raw is None else {
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
        return {**base, "mode": "preview"}

    expected_token = str(payload.get("state_token", ""))
    if expected_token != state_token:
        raise ValueError("provider result changed after preview; preview again")
    actor = _optional_text(payload.get("actor"))
    reason = _optional_text(payload.get("reason"))
    if action == "reviewed_no_match":
        from .catalogs import CatalogService

        result = CatalogService(session_factory, {}).override_no_match(
            run_id, actor=actor, reason=reason,
        )
    else:
        if catalog_service_factory is None:
            raise RuntimeError(
                f"{action.replace('_', ' ')} is unavailable in this review server"
            )
        service = catalog_service_factory(provider, action)
        if action == "accept_candidate":
            result = service.override_candidate(
                raw_row_id, actor=actor, reason=reason,
            )
        else:
            result = service.retry_failed_run(
                run_id, actor=actor, reason=reason,
            )
    return {
        **base,
        "mode": "applied",
        "applied": result.__dict__,
    }


def _with_human_summary(
    value: dict[str, object], summary: dict[str, object],
) -> dict[str, object]:
    return {**value, "human_summary": summary}


def _provider_result_summary(value: dict[str, object]) -> dict[str, object]:
    action = str(value["action"])
    provider = str(value["run"]["provider"])
    candidate = value.get("candidate")
    if action == "accept_candidate":
        change = (
            f"Accept {provider} source {candidate['source_id']} as the catalog match."
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
            "Retry performs a provider request and records its new versioned result."
        )
    return {
        "title": title,
        "facts": [
            f"Target: {value['target']['sdbid']}",
            f"Current result: {provider} — {value['run']['status']}",
        ],
        "changes": [change],
        "warnings": warnings,
    }


def _catalog_association_summary(
    value: dict[str, object],
) -> dict[str, object]:
    detection = value["detection"]
    target = value["target"]
    action = str(value["action"])
    verb = "Accept" if action == "accept" else "Reject"
    source = (
        detection.get("source_display_name") or detection["source_id"]
    )
    change = (
        f"{verb} {detection['provider']} source {source} "
        f"{'as' if action == 'accept' else 'for'} {target['sdbid']}."
    )
    if value["mode"] == "applied":
        added = int((value.get("applied") or {}).get("actions_added", 0))
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
            (
                f"Separation: "
                f"{float(detection['separation_arcsec']):.2f} arcsec"
            ),
            "The original provider query and raw result remain unchanged.",
            (
                "Measurement contributor/composite assignment is a separate "
                "decision."
            ),
        ],
        "changes": [change] if value["has_changes"] else [
            f"The latest manual decision is already {action}."
        ],
        "warnings": [],
    }


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
            f"Detection: {detection['provider']} "
            f"{detection.get('source_display_name') or detection['source_id']}",
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
        f"Applied {int(applied.get('actions_added', 0))} fit include/exclude change"
        f"{'s' if int(applied.get('actions_added', 0)) != 1 else ''}"
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
        "changes": changes or ["The effective measurement settings already match."],
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
    decision = DecisionContext.resolve(
        actor=_optional_text(payload.get("actor")),
        reason=_optional_text(payload.get("reason")),
        suggested_reason=str(preview["suggested_reason"]),
    )
    result = import_immediate_relatives(
        session_factory,
        target,
        identity_service=identity_service_factory(),
        actor=decision.actor,
        reason=decision.reason,
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
        "relationship_ids": row["relationship_ids"],
        "action": row["action"],
        "matched_target_id": row["matched_target_id"],
        "component_label": row["component_label"],
        "reconciliation_missing": row.get("reconciliation_missing", []),
        "suggested_role": row["suggested_role"],
        "suggested_state": row["suggested_state"],
    } for row in rows]
    token = hashlib.sha256(
        json.dumps(token_rows, sort_keys=True).encode("utf-8")
    ).hexdigest()
    counts = {
        action: sum(row["action"] == action for row in rows)
        for action in (
            "import", "reconcile", "complete",
            "context_only", "review_required",
        )
    }
    value = {
        "mode": "preview",
        "target": str(target_reference),
        "state_token": token,
        "has_changes": bool(counts["import"] or counts["reconcile"]),
        "counts": counts,
        "relatives": rows,
        "suggested_reason": (
            f"Imported and reconciled immediate stellar relatives for {target_reference}"
        ),
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
        elif row["action"] == "reconcile":
            changes.append(
                f"Reconcile existing {row['matched_sdbid']} with {label}{component}."
            )
        elif row["action"] == "reconciled":
            changes.append(
                f"Reconciled existing {row['matched_sdbid']} with {label}{component}."
            )
        elif row["action"] == "complete":
            changes.append(
                f"No change for {row['matched_sdbid']}; {label}{component} is "
                "already reconciled."
            )
        elif row["action"] == "review_required":
            warnings.append(f"Review required: {label} — {row['reason']}")
        elif row["action"] == "context_only":
            relevance = {
                "contextual_group": "contextual group",
                "planetary_or_disk": "planet",
                "stellar_or_substellar_component": "stellar",
            }.get(
                str(row["component_relevance"]),
                str(row["component_relevance"]).replace("_", " "),
            )
            warnings.append(f"Context only: {label} — {relevance}")
        elif row["action"] == "failed":
            warnings.append(f"Import failed: {label} — {row.get('error', 'unknown error')}")
    if value.get("mode") == "applied":
        title = (
            f"Relative import finished: {int(value.get('imported', 0))} imported, "
            f"{int(value.get('reconciled', 0))} reconciled, "
            f"{int(value.get('already_complete', 0))} already complete, "
            f"{int(value.get('failed', 0))} failed"
        )
    else:
        title = "SIMBAD-relative changes ready" if value["has_changes"] else "No relatives to import"
    return {
        "title": title,
        "facts": [
            f"Target: {value['target']}",
            "Only immediate stellar relatives are imported; expansion is not recursive.",
        ],
        "changes": changes or ["No immediate stellar relatives need importing or reconciliation."],
        "warnings": warnings,
    }


def _nearby_import_search_payload(
    session_factory: sessionmaker[Session],
    identity_service_factory: Callable[[], IdentityService] | None,
    payload: dict[str, object],
) -> dict[str, object]:
    if identity_service_factory is None:
        raise RuntimeError(
            "nearby SIMBAD search is unavailable in offline review mode"
        )
    raw_radius = payload.get("radius_arcsec", 60)
    if isinstance(raw_radius, bool):
        raise ValueError("search radius must be a number")
    radius_arcsec = float(raw_radius)
    identity = identity_service_factory()
    provider = identity.simbad
    if not hasattr(provider, "search_region"):
        raise RuntimeError("the configured SIMBAD provider cannot search by position")
    result = search_nearby_simbad(
        session_factory,
        str(payload["target"]),
        provider=provider,
        radius_arcsec=radius_arcsec,
    )
    value = result.as_dict()
    value["new_count"] = sum(
        bool(row["selectable"]) for row in value["candidates"]
    )
    value["existing_count"] = sum(
        row["existing_sdbid"] is not None for row in value["candidates"]
    )
    value["blocked_count"] = sum(
        row["blocked_reason"] is not None
        and row["existing_sdbid"] is None
        for row in value["candidates"]
    )
    return value


def _nearby_import_from_payload(
    session_factory: sessionmaker[Session],
    identity_service_factory: Callable[[], IdentityService] | None,
    update_factory: Callable[[], object] | None,
    catalog_providers: tuple[str, ...] | None,
    payload: dict[str, object],
) -> dict[str, object]:
    if identity_service_factory is None or update_factory is None:
        raise RuntimeError(
            "nearby target import is unavailable in this review server"
        )
    raw_names = payload.get("main_ids")
    if not isinstance(raw_names, list):
        raise ValueError("main_ids must be a list")
    names = tuple(dict.fromkeys(
        str(value).strip() for value in raw_names if str(value).strip()
    ))
    if not names:
        raise ValueError("select at least one SIMBAD object to import")
    if len(names) > 50:
        raise ValueError("at most 50 SIMBAD objects may be imported at once")
    source_target = str(payload["target"])
    providers = tuple(dict.fromkeys((
        "simbad",
        *(catalog_providers or ()),
    )))
    result = TargetImportService(
        session_factory,
        identity_service=identity_service_factory(),
        update_service=update_factory(),
    ).import_many(
        names,
        providers=providers,
        command=f"review import near {source_target}",
    )
    value = {
        **result.as_dict(),
        "mode": "applied",
        "source_target": source_target,
    }
    return _with_human_summary(value, _nearby_import_summary(value))


def _nearby_import_summary(
    value: dict[str, object],
) -> dict[str, object]:
    changes = []
    warnings = []
    for item in value["items"]:
        if item["status"] == "failed":
            warnings.append(
                f"{item['requested_name']}: {item.get('error') or 'import failed'}"
            )
        else:
            changes.append(
                f"{item['requested_name']}: {item['status']} as {item['sdbid']}"
            )
    update = value.get("update_summary") or {}
    for item in update.get("items", []):
        if item["action"] in {"failed", "missing"}:
            detail = f" — {item['detail']}" if item.get("detail") else ""
            warnings.append(
                f"{item.get('sdbid') or 'target'}: "
                f"{item['provider']} {item['action']}{detail}"
            )
    return {
        "title": (
            f"Nearby import finished: {value['created_count']} created, "
            f"{value['existing_count']} existing, "
            f"{value['failed_count']} failed"
        ),
        "facts": [
            f"Search target: {value['source_target']}",
            "Provider coverage and stored WDS/CCDM matching were run for successful targets.",
        ],
        "changes": changes or ["No targets were imported."],
        "warnings": warnings,
    }


def _catalog_coverage_from_payload(
    session_factory: sessionmaker[Session],
    providers: tuple[str, ...] | None,
    update_factory: Callable[[], object] | None,
    catalog_service_factory: Callable[[str, str], object] | None,
    payload: dict[str, object],
    *,
    apply: bool,
) -> dict[str, object]:
    target = str(payload["target"])
    preview = _catalog_coverage_preview_payload(
        session_factory,
        target,
        providers=providers,
        update_available=update_factory is not None,
        normalization_available=catalog_service_factory is not None,
    )
    if not apply:
        return preview
    if not preview["action_available"]:
        raise RuntimeError(
            "catalog coverage changes are unavailable in this review server"
        )
    expected_token = str(payload.get("state_token") or "")
    if not expected_token or expected_token != preview["state_token"]:
        raise RuntimeError(
            "catalog coverage changed after preview; reload and preview again"
        )
    missing_rows = [
        row for row in preview["coverage"] if row["missing_providers"]
    ]
    normalization_gaps = {
        int(gap["detection_id"]): gap
        for row in preview["coverage"]
        for gap in row["normalization_gaps"]
    }
    if not missing_rows and not normalization_gaps:
        return preview
    normalization_results = []
    if normalization_gaps and catalog_service_factory is not None:
        by_provider: dict[str, list[int]] = {}
        for detection_id, gap in normalization_gaps.items():
            by_provider.setdefault(str(gap["provider"]), []).append(
                detection_id
            )
        for provider, detection_ids in by_provider.items():
            summary = catalog_service_factory(
                provider, "normalize"
            ).normalize_detections(detection_ids)
            normalization_results.append({
                "provider": provider,
                "detection_count": summary.detection_count,
                "completed": summary.completed,
                "no_measurements": summary.no_measurements,
                "failed": summary.failed,
                "measurement_count": summary.measurement_count,
                "items": [
                    {
                        "detection_id": item.detection_id,
                        "provider": item.provider,
                        "source_id": item.source_id,
                        "status": item.status,
                        "measurement_count": item.measurement_count,
                        "error": item.error,
                    }
                    for item in summary.items
                ],
            })
    provider_set = {
        provider
        for row in missing_rows
        for provider in row["missing_providers"]
    }
    selected_providers = tuple(
        provider
        for provider in preview["expected_providers"]
        if provider in provider_set
    )
    summary = None
    if missing_rows and update_factory is not None:
        summary = update_factory().update_targets(
            [str(row["target_sdbid"]) for row in missing_rows],
            providers=selected_providers,
            force=False,
        )
    refreshed = _catalog_coverage_preview_payload(
        session_factory,
        target,
        providers=providers,
        update_available=update_factory is not None,
        normalization_available=catalog_service_factory is not None,
    )
    value = {
        **refreshed,
        "mode": "applied",
        "normalization_applied": normalization_results,
        "applied": (
            None if summary is None else {
                "target_count": int(summary.target_count),
                "refreshed": int(summary.refreshed),
                "skipped": int(summary.skipped),
                "missing": int(summary.missing),
                "failed": int(summary.failed),
                "items": [
                    {
                        "target_id": item.target_id,
                        "sdbid": item.sdbid,
                        "provider": item.provider,
                        "action": item.action,
                        "status": item.status,
                        "detail": item.detail,
                    }
                    for item in summary.items
                ],
            }
        ),
    }
    return _with_human_summary(value, _catalog_coverage_summary(value))


def _catalog_coverage_preview_payload(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    providers: tuple[str, ...] | None,
    update_available: bool,
    normalization_available: bool,
) -> dict[str, object]:
    context = HierarchyService(session_factory).system_context(
        target_reference,
        catalog_providers=providers,
    )
    names = dict(context.get("simbad_main_id_by_target", {}))
    coverage = [
        {
            **row,
            "display_name": names.get(
                str(row["target_sdbid"]), str(row["target_sdbid"])
            ),
        }
        for row in context.get("catalog_coverage_by_target", [])
    ]
    token_rows = [{
        "target_id": row["target_id"],
        "target_sdbid": row["target_sdbid"],
        "current_providers": row["current_providers"],
        "missing_providers": row["missing_providers"],
        "failed_providers": row["failed_providers"],
        "normalization_gaps": row["normalization_gaps"],
    } for row in coverage]
    token = hashlib.sha256(
        json.dumps(token_rows, sort_keys=True).encode("utf-8")
    ).hexdigest()
    expected = (
        list(coverage[0]["expected_providers"]) if coverage else list(providers or ())
    )
    missing_count = sum(
        len(row["missing_providers"]) for row in coverage
    )
    normalization_ids = {
        int(gap["detection_id"])
        for row in coverage
        for gap in row["normalization_gaps"]
    }
    normalization_count = len(normalization_ids)
    action_available = bool(
        (missing_count and update_available)
        or (normalization_count and normalization_available)
    )
    value = {
        "mode": "preview",
        "target": str(target_reference),
        "state_token": token,
        "has_changes": bool(missing_count or normalization_count),
        "update_available": update_available,
        "normalization_available": normalization_available,
        "action_available": action_available,
        "expected_providers": expected,
        "missing_count": missing_count,
        "normalization_count": normalization_count,
        "coverage": coverage,
    }
    return _with_human_summary(value, _catalog_coverage_summary(value))


def _catalog_coverage_summary(value: dict[str, object]) -> dict[str, object]:
    coverage = value["coverage"]
    expected_count = len(value["expected_providers"])
    changes = [
        f"Normalize stored {gap['provider']} candidate {gap['source_id']}."
        for gap in {
            int(gap["detection_id"]): gap
            for row in coverage
            for gap in row["normalization_gaps"]
        }.values()
    ]
    changes.extend(
        f"Query {', '.join(row['missing_providers'])} for "
        f"{row['display_name']}."
        for row in coverage
        if row["missing_providers"]
    )
    failures = [
        f"{row['display_name']}: previous failed attempt for "
        f"{', '.join(row['failed_providers'])}."
        for row in coverage
        if row["failed_providers"]
    ]
    applied = value.get("applied")
    normalization_applied = value.get("normalization_applied") or []
    normalized_count = sum(
        row["completed"] + row["no_measurements"]
        for row in normalization_applied
    )
    normalization_failed = sum(
        row["failed"] for row in normalization_applied
    )
    if applied or normalization_applied:
        provider_result = (
            "no provider requests run"
            if not applied
            else (
                f"{applied['refreshed']} refreshed, "
                f"{applied['skipped']} skipped, {applied['failed']} failed"
            )
        )
        title = (
            f"Catalog work finished: {normalized_count} stored detections processed; "
            f"{provider_result}"
        )
        if applied:
            failures.extend(
                f"{item['sdbid'] or 'system'} · {item['provider']}: "
                f"{item['detail'] or item['status']}"
                for item in applied["items"]
                if item["action"] in {"failed", "missing"}
            )
        if normalization_failed:
            failures.append(
                f"{normalization_failed} stored detections could not be normalized."
            )
    elif value["missing_count"]:
        title = f"{value['missing_count']} catalog requests missing"
        if value["normalization_count"]:
            title += (
                f"; {value['normalization_count']} detections need normalization"
            )
    elif value["normalization_count"]:
        title = (
            f"{value['normalization_count']} stored detections need normalization"
        )
    else:
        title = "Catalog coverage is complete"
    warnings = failures
    if value["missing_count"] and not value["update_available"]:
        warnings.append(
            "This server cannot query missing remote providers in offline mode."
        )
    return {
        "title": title,
        "facts": [
            f"System targets: {len(coverage)}",
            f"Expected providers per target: {expected_count}",
            "A no-match result counts as completed coverage.",
            "Stored candidate photometry is normalized without querying the provider again.",
        ],
        "changes": changes or ["No direct catalog provider requests are missing."],
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
            str(row.get("display_name") or ""),
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
        provider_text = ", ".join(
            str(value["provider"]) for value in row["providers"]
        )
        target_query = _queue_query(filters, position)
        target_url = (
            f"/target/{quote(str(row['sdbid']))}{_e(target_query)}"
        )
        display_name_html = (
            f"<a href='{target_url}'>{_e(row['display_name'])}</a>"
            if row.get("display_name") else "<span class='muted'>—</span>"
        )
        rows.append(
            f"<tr class='priority-{_e(row['priority'])}' data-classification='{_e(row['classification'])}'>"
            f"<td>{_e(row['priority'])}</td>"
            f"<td>{display_name_html}</td>"
            f"<td><a href='{target_url}'><code>{_e(row['sdbid'])}</code></a></td>"
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
    <label>Search <input name="search" value="{_e(filters.get('search', ''))}" placeholder="name, target, band, action"></label>
    <div class="filter-actions"><button type="submit">Apply filters</button><a href="/">Clear</a></div>
  </form>
  <p class="muted queue-count">Showing <strong>{len(filtered_rows)}</strong> of {len(all_rows)} sample targets.</p>
  <table><thead><tr><th>priority</th><th>SIMBAD name</th><th>SDB ID</th><th>classification</th><th>role</th><th>detections</th><th>unassigned / mixed</th><th>providers</th><th>recommended action</th></tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="9" class="muted">No sample targets match these filters.</td></tr>'}</tbody></table>
</main>"""
    return _page(f"SDB review: {sample}", body)


def _target_external_resources(
    identifier: str,
    *,
    ra_deg: object,
    dec_deg: object,
) -> list[dict[str, str]]:
    resources = [{
        "label": "SIMBAD",
        "title": "SIMBAD",
        "url": (
            "https://simbad.cds.unistra.fr/simbad/sim-id?"
            + urlencode({
                "submit": "submit id",
                "Ident": identifier,
            })
        ),
    }]
    if ra_deg is None or dec_deg is None:
        return resources
    ra = str(float(ra_deg))
    dec = str(float(dec_deg))
    coordinate = f"{ra} {dec}"
    comma_coordinate = f"{ra},{dec}"
    resources.extend([
        {
            "label": "CDS",
            "title": "CDS Portal",
            "url": (
                "https://cdsportal.u-strasbg.fr/?"
                + urlencode({"target": coordinate})
            ),
        },
        {
            "label": "CASSIS",
            "title": "Cornell Atlas of Spitzer IRS Sources",
            "url": (
                "https://cassis.sirtf.com/atlas/cgi/radec.py?"
                + urlencode({"ra": ra, "dec": dec, "radius": 20})
            ),
        },
        {
            "label": "Finder",
            "title": "IRSA Finder Chart",
            "url": (
                "https://irsa.ipac.caltech.edu/applications/finderchart/"
                "servlet/api?"
                + urlencode({
                    "mode": "getResult",
                    "locstr": comma_coordinate,
                })
            ),
        },
        {
            "label": "Spitzer",
            "title": "Spitzer Heritage Archive",
            "url": (
                "https://sha.ipac.caltech.edu/applications/Spitzer/SHA/?"
                + urlencode({
                    "api": "search",
                    "searchoption": "POSITION",
                    "sr": "180s",
                    "WorldPt": f"{ra};{dec};EQ_J2000",
                    "execute": "true",
                })
            ),
        },
        {
            "label": "MAST",
            "title": "MAST Portal",
            "url": (
                "https://mast.stsci.edu/portal/Mashup/Clients/Mast/"
                "Portal.html?"
                + urlencode({"searchQuery": comma_coordinate})
            ),
        },
        {
            "label": "ESASky",
            "title": "ESASky",
            "url": (
                "https://sky.esa.int/?"
                + urlencode({
                    "action": "goto",
                    "fov": "0.25",
                    "cooframe": "J2000",
                    "sci": "true",
                    "hips": "AllWISE color",
                    "target": coordinate,
                })
            ),
        },
    ])
    return resources


def _target_page(
    sdbid: str,
    readiness: dict[str, object],
    graph: dict[str, object],
    raw_row_detections: dict[int, int],
    navigation: dict[str, object] | None = None,
    display_name: str | None = None,
    simbad_main_ids: dict[str, str] | None = None,
    catalog_coverage: list[dict[str, object]] | None = None,
    catalog_update_available: bool = False,
    nearby_import_available: bool = False,
    target_position: dict[str, object] | None = None,
) -> str:
    default_actor = os.environ.get("SDB_ACTOR", "").strip()
    target = next(
        (row for row in graph["targets"] if row["sdbid"] == sdbid),
        None,
    )
    if target is None:
        raise KeyError(f"target is not present in its fitting graph: {sdbid}")
    targets = sorted(graph["targets"], key=lambda row: str(row["sdbid"]))
    simbad_main_ids = simbad_main_ids or {}

    def target_label(row: dict[str, object]) -> str:
        target_sdbid = str(row["sdbid"])
        return simbad_main_ids.get(target_sdbid, target_sdbid)

    def source_html(
        label: object,
        rows: list[dict[str, object]],
    ) -> str:
        for row in rows:
            access_url = str(row.get("access_url") or "")
            if access_url.startswith("https://"):
                return (
                    f"<a href='{_e(access_url)}' target='_blank' "
                    f"rel='noopener'>{_e(label)}</a>"
                )
        return _e(label)

    requested_target_label = simbad_main_ids.get(sdbid, display_name or sdbid)
    target_position = target_position or {}
    external_resources = _target_external_resources(
        requested_target_label,
        ra_deg=target_position.get("ra2000_deg"),
        dec_deg=target_position.get("dec2000_deg"),
    )
    external_resource_html = "".join(
        f"<a class='external-resource' href='{_e(row['url'])}' target='_blank' "
        f"rel='noopener' title='{_e(row['title'])}'>{_e(row['label'])}</a>"
        for row in external_resources
    )
    catalog_coverage = catalog_coverage or []
    coverage_missing = sum(
        len(row["missing_providers"]) for row in catalog_coverage
    )
    coverage_total = sum(
        int(row["expected_count"]) for row in catalog_coverage
    )
    coverage_current = coverage_total - coverage_missing
    coverage_normalization = len({
        int(gap["detection_id"])
        for row in catalog_coverage
        for gap in row.get("normalization_gaps", [])
    })
    coverage_label = (
        f"Catalog coverage {coverage_current}/{coverage_total}"
        if coverage_total
        else "Catalog coverage"
    )
    if coverage_normalization:
        coverage_label += f" · {coverage_normalization} to normalize"
    detection_rows: dict[int, list[dict[str, object]]] = defaultdict(list)
    for measurement in graph["measurements"]:
        detection_rows[int(measurement["detection_id"])].append(measurement)
    cards = []
    for detection_id, measurements in sorted(
        detection_rows.items(),
        key=lambda item: (item[1][0]["provider"], item[1][0]["source_id"]),
    ):
        first = measurements[0]
        source = source_html(
            first.get("source_display_name") or first["source_id"],
            list(first.get("provenance") or []),
        )
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
        ordinary_default = (
            not mixed_assignments
            and len(current_contributors) == 1
            and not current_scopes
            and all(
                len(row["assignments"]) == 1
                and row["assignments"][0].get("derived") is True
                and row["assignments"][0]["role"] == "contributor"
                for row in measurements
            )
        )
        target_choices = "".join(
            f"<label><input type='checkbox' class='contributor' "
            f"value='{_e(row['sdbid'])}'"
            f"{' checked' if row['sdbid'] in current_contributors else ''}> "
            f"<code>{_e(target_label(row))}</code> ({_e(row['role'])})</label>"
            for row in targets
            if row["model_target"] or row["sdbid"] in all_current_contributors
        )
        default_scope = next(iter(sorted(current_scopes)), None)
        if default_scope is None:
            default_scope = (
                sdbid if sdbid in all_current_scopes
                else str(first["origin_sdbid"] or sdbid)
            )
        has_combined_system = len(targets) > 1
        scope_choices = "".join(
            f"<option value='{_e(row['sdbid'])}'"
            f"{' selected' if row['sdbid'] == default_scope else ''}>"
            f"{_e(target_label(row))} ({_e(row['role'])})</option>"
            for row in targets
        )
        combined_system_control = (
            "<div class='combined-system-control'>"
            "<label><input type='checkbox' class='composite-scope'"
            f"{' checked' if default_scope in current_scopes else ''}> "
            "Measurement applies to the combined system</label>"
            f"<label class='scope-target-field'"
            f"{'' if default_scope in current_scopes else ' hidden'}>"
            f"System target <select class='scope-target'>{scope_choices}</select></label>"
            "</div>"
            if has_combined_system
            else ""
        )
        band_rows = []
        for row in measurements:
            excluded = bool(row["fit_excluded"])
            basis = str(row["exclusion_basis"] or "")
            status = "Excluded from fit" if excluded else "Included in fit"
            basis_label = {
                "provider_excluded": "provider default",
                "manual_exclude_action": "manual decision",
                "manual_include_action": "manual decision",
                "shared_detection": "shared-source safety",
                "iras_alternate": "IRAS duplicate safety",
                "tdsc_preferred": "TDSC preferred",
            }.get(basis, "")
            if basis_label:
                status += f" · {basis_label}"
            band_rows.append(
                f"<div class='band-row'><label><input type='checkbox' "
                f"class='measurement' value='{row['measurement_id']}' checked> "
                f"{_e(row['band'])}: {_display_number(row['value'])} ± "
                f"{_display_number(row['error'])} {_e(row['unit'])}</label>"
                f"<span class='eligibility-state {'excluded' if excluded else 'included'}'"
                f" data-current-label='{_e(status)}'>{_e(status)}</span>"
                f"<button type='button' class='eligibility-toggle' "
                f"data-current-excluded='{str(excluded).lower()}' "
                f"data-desired-excluded='{str(excluded).lower()}' "
                f"data-measurement='{row['measurement_id']}' "
                f"aria-pressed='false'>"
                f"{'Include in fit' if excluded else 'Exclude from fit'}</button></div>"
            )
        bands = "".join(band_rows)
        contributor_editor = (
            f"<h4>Contributors</h4><div class='choices'>{target_choices}</div>"
            f"{combined_system_control}"
        )
        if ordinary_default:
            default_sdbid = next(iter(current_contributors))
            default_target = next(
                (row for row in targets if row["sdbid"] == default_sdbid),
                None,
            )
            default_label = (
                default_sdbid
                if default_target is None
                else target_label(default_target)
            )
            attribution = (
                "<p class='assignment-default'>Photometry follows the accepted "
                f"source association to <code>{_e(default_label)}</code>. "
                "No separate assignment decision is needed.</p>"
                "<details class='attribution-exception'><summary>Change attribution"
                " (exception)</summary>"
                f"{contributor_editor}"
                "<button class='preview'>Preview attribution change</button>"
                "</details>"
                "<div class='drawer-actions'><button class='preview-eligibility' "
                "type='button'>Preview include/exclude</button></div>"
            )
        else:
            attribution = (
                contributor_editor
                + "<div class='drawer-actions'><button class='preview'>"
                "Preview decision</button><button class='preview-eligibility' "
                "type='button'>Preview include/exclude</button></div>"
            )
        cards.append(f"""
<section class="detection" data-detection="{detection_id}">
  <h3>{_e(first['provider'])} · {source}</h3>
  <div class="bands">{bands}</div>
  {"<p class='warning'>Bands currently have different assignments. Their common assignments are selected below; preview carefully before applying.</p>" if mixed_assignments else ""}
  {attribution}
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
    <strong>{f'{_e(display_name)} · ' if display_name else ''}<code>{_e(sdbid)}</code></strong>
    <span class="muted">{_e(target['role'])}/{_e(target['state'])} · {readiness_text}</span>
    {navigation_html}
    <div class="header-actions">
      {external_resource_html}
      <button id="nearby-import" type="button"{'' if nearby_import_available else ' disabled'} title="{'Search SIMBAD around this target and import selected objects' if nearby_import_available else 'Nearby import is unavailable in offline review mode'}">Import nearby</button>
      <button id="catalog-coverage" class="{'needs-attention' if coverage_missing else ''}" type="button">{_e(coverage_label)}</button>
      <button id="classify-target" class="{'needs-decision' if target['role'] == 'unspecified' else ''}" type="button">{'Decide target role' if target['role'] == 'unspecified' else 'Change target role'}</button>
    </div>
  </header>
  <iframe id="sky-review" title="Sky and system review for {_e(sdbid)}" src="/target/{quote(sdbid)}/sky"></iframe>
  <aside id="assignment-drawer" class="assignment-drawer" hidden>
    <div class="drawer-header"><div><h2 id="drawer-title">Review tools</h2><div id="selected-source" class="muted"></div></div><button id="close-drawer" type="button" aria-label="Close review drawer">×</button></div>
    <p id="assignment-prompt" class="muted">Select a plotted catalog source to review it.</p>
    <section class="decision-meta"><label>Actor <input id="actor" value="{_e(default_actor)}"></label><label>Reason <input id="reason" placeholder="Preview suggests a reason"></label></section>
    <section id="catalog-association-editor" class="detection" hidden>
      <h3>Source association</h3>
      <p id="catalog-association-context" class="muted"></p>
      <div class="drawer-actions">
        <button type="button" class="preview-catalog-association" data-action="accept">Accept for this target</button>
        <button type="button" class="preview-catalog-association" data-action="reject">Reject for this target</button>
      </div>
    </section>
    <section id="catalog-association-preview-panel" class="preview-panel" hidden><h2>Source association preview</h2><div id="catalog-association-preview" class="change-summary muted">Choose an action, then preview.</div><button id="apply-catalog-association" disabled>Apply source association</button></section>
    <div id="detection-editors">{''.join(cards) or '<p>No current measurements.</p>'}</div>
    <section id="provider-result-editor" class="detection" hidden>
      <h3>Provider result</h3>
      <p id="provider-result-context" class="muted"></p>
      <div class="drawer-actions">
        <button type="button" class="preview-provider-result" data-action="accept_candidate">Preview accept candidate</button>
        <button type="button" class="preview-provider-result" data-action="reviewed_no_match">Preview no match</button>
        <button type="button" class="preview-provider-result" data-action="retry">Preview retry</button>
      </div>
    </section>
    <section id="provider-result-preview-panel" class="preview-panel" hidden><h2>Provider result preview</h2><div id="provider-result-preview" class="change-summary muted">Choose an action, then preview.</div><button id="apply-provider-result" disabled>Apply provider result action</button></section>
    <div class="preview-grid">
      <section class="preview-panel"><h2>Decision preview</h2><div id="preview" class="change-summary muted">Choose assignments, then preview.</div><button id="apply" disabled>Apply audited decision</button></section>
      <section class="preview-panel"><h2>Fit include/exclude preview</h2><p class="muted">Fit inclusion is independent of component ownership. Changes apply by origin target, provider, and band.</p><div id="eligibility-preview" class="change-summary muted">Choose a band action, then preview.</div><button id="apply-eligibility" disabled>Apply include/exclude changes</button></section>
    </div>
  </aside>
  <dialog id="lifecycle-dialog">
    <form method="dialog" class="dialog-header"><div><h2>Target modelling role</h2><code>{_e(requested_target_label)}</code></div><button value="cancel" aria-label="Close target role dialog">×</button></form>
    <p>This decision describes how the target participates in fitting; it does not assert whether the object is single or multiple.</p>
    <label class="role-choice"><input type="radio" name="lifecycle-role" value="physical"{' checked' if target['role'] != 'composite' else ''}> <strong>Physical / fitted model</strong><span>Fit one photospheric model for this target. Use this for an unresolved combined-light AB system when A and B are not separately modelled; WDS multiplicity remains recorded.</span></label>
    <label class="role-choice"><input type="radio" name="lifecycle-role" value="composite"{' checked' if target['role'] == 'composite' else ''}> <strong>Composite / measurement scope</strong><span>Do not fit this target itself. Its measurements must be assigned to separately imported physical contributors such as A and B.</span></label>
    <p id="lifecycle-warning" class="warning" hidden>A composite without imported physical contributors will remain unresolved for joint fitting. Choose physical if one combined-light model is the intended approximation.</p>
    <section class="decision-meta"><label>Actor <input id="lifecycle-actor" value="{_e(default_actor)}"></label><label>Reason <input id="lifecycle-reason" placeholder="Preview suggests a reason"></label></section>
    <div class="dialog-actions"><button id="preview-lifecycle" type="button">Preview role decision</button><button id="apply-lifecycle" type="button" disabled>Apply audited decision</button></div>
    <div id="lifecycle-preview" class="change-summary muted">Choose a role, then preview.</div>
  </dialog>
  <dialog id="relatives-dialog">
    <form method="dialog" class="dialog-header"><div><h2>Immediate SIMBAD relatives</h2><code>{_e(requested_target_label)}</code></div><button value="cancel" aria-label="Close relative import dialog">×</button></form>
    <p>Preview or import only immediate stellar parents and children. Contextual groups, planets, and unknown object types are retained for review but are not imported; newly imported targets are not expanded recursively.</p>
    <section class="decision-meta"><label>Actor <input id="relatives-actor" value="{_e(default_actor)}"></label><label>Reason <input id="relatives-reason" placeholder="Preview suggests a reason"></label></section>
    <div class="dialog-actions"><button id="preview-relatives" type="button">Refresh preview</button><button id="apply-relatives" type="button" disabled>Import and reconcile stellar relatives</button></div>
    <div id="relatives-preview" class="change-summary muted">Open this dialog from Immediate SIMBAD relatives in the system column.</div>
  </dialog>
  <dialog id="nearby-import-dialog" class="nearby-import-dialog">
    <form method="dialog" class="dialog-header"><div><h2>Import nearby SIMBAD objects</h2><code>{_e(requested_target_label)}</code></div><button value="cancel" aria-label="Close nearby import dialog">×</button></form>
    <p>Search around the target position, select new objects, then import them with configured provider coverage. Angular proximity alone does not create a system relationship or sample membership.</p>
    <div class="nearby-search-controls">
      <label>Radius <span><input id="nearby-import-radius" type="number" min="1" max="600" step="1" value="60"> arcsec</span></label>
      <button id="search-nearby-import" type="button">Search SIMBAD</button>
    </div>
    <div id="nearby-import-search-status" class="change-summary muted">Search to list nearby SIMBAD objects.</div>
    <div id="nearby-import-results" class="nearby-import-results" hidden>
      <table><thead><tr><th>Import</th><th>SIMBAD ID</th><th>Type</th><th>Spectral type</th><th>d</th><th>SDB status</th></tr></thead><tbody id="nearby-import-rows"></tbody></table>
    </div>
    <div class="dialog-actions"><button id="apply-nearby-import" type="button" disabled>Import selected</button></div>
    <div id="nearby-import-summary" class="change-summary muted"></div>
    <div id="nearby-import-target-links" class="import-target-links"></div>
  </dialog>
  <dialog id="catalog-coverage-dialog">
    <form method="dialog" class="dialog-header"><div><h2>Catalog coverage</h2><code>{_e(requested_target_label)}</code></div><button value="cancel" aria-label="Close catalog coverage dialog">×</button></form>
    <p>Normalize stored catalog candidates and complete direct provider searches for system targets that do not yet have a current result. Existing results are skipped.</p>
    <div class="dialog-actions"><button id="preview-catalog-coverage" type="button">Refresh preview</button><button id="apply-catalog-coverage" type="button" disabled>Complete catalog gaps</button></div>
    <div id="catalog-coverage-preview" class="change-summary muted">Open this dialog to check direct provider coverage.</div>
  </dialog>
</main>
<script>window.SDB_TARGET={json.dumps(sdbid)};window.SDB_TARGET_NAMES={json.dumps(simbad_main_ids, sort_keys=True)};window.SDB_RAW_ROW_DETECTIONS={json.dumps(raw_row_detections, sort_keys=True)};window.SDB_CATALOG_UPDATE_AVAILABLE={json.dumps(catalog_update_available)};</script>
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
body{font-family:system-ui,-apple-system,sans-serif;color:#172033;margin:0;background:#f6f8fb}main{padding:22px;max-width:1600px;margin:auto}a{color:#2357a6}code{font-family:ui-monospace,monospace}.muted{color:#64748b}.summary{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}.summary span,.decision-meta,.detection,.preview-panel{background:white;border:1px solid #dce3ec;border-radius:8px;padding:12px}table{border-collapse:collapse;width:100%;background:white}th,td{padding:8px;border-bottom:1px solid #e5eaf0;text-align:left;vertical-align:top}.priority-highest{background:#fff1e8}.priority-high{background:#fffbea}.queue-filters{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr)) minmax(180px,1.5fr) auto;gap:10px;align-items:end;background:white;border:1px solid #dce3ec;border-radius:8px;padding:12px}.queue-filters select,.queue-filters input{box-sizing:border-box;width:100%;min-height:34px}.filter-actions{display:flex;gap:10px;align-items:center;min-height:34px}.queue-count{margin:10px 0}.detection{display:none;margin:12px 0}.detection.active{display:block}.bands,.choices{display:grid;gap:7px;margin:8px 0 12px}.band-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 8px;padding-bottom:7px;border-bottom:1px solid #eef2f7}.band-row .eligibility-state{font-size:12px;align-self:center}.band-row .included{color:#28734a}.band-row .eligibility{grid-column:1/-1;width:100%}.assignment-default{background:#edf8f1;border-left:4px solid #3b8b5d;padding:9px}.attribution-exception{margin:10px 0}.attribution-exception summary{color:#475569;cursor:pointer}.decision-meta{display:grid;grid-template-columns:1fr;gap:10px;margin:12px 0}.decision-meta input{box-sizing:border-box;width:100%}label{display:block}.excluded{color:#a12828}.warning{background:#fff8df;border-left:4px solid #d99b16;padding:8px}button{padding:7px 12px}pre{white-space:pre-wrap;max-height:34vh;overflow:auto;font-size:12px}.change-summary{margin:10px 0}.change-summary h3{margin:0 0 8px;color:#172033}.change-summary ul{margin:7px 0;padding-left:20px}.change-summary .summary-warning{color:#92400e}.change-summary details{margin-top:10px}.change-summary details pre{background:#f1f5f9;border-radius:6px;padding:8px;color:#334155}.live-review{overflow:hidden;--drawer-width:min(420px,38vw)}.live-workspace{padding:0;max-width:none;height:100vh}.live-header{height:48px;box-sizing:border-box;display:flex;gap:12px;align-items:center;padding:8px 12px;background:#fff;border-bottom:1px solid #dce3ec;white-space:nowrap;overflow:hidden}.live-header .muted{min-width:0;overflow:hidden;text-overflow:ellipsis}.header-actions{display:flex;flex-shrink:0;gap:4px;align-items:center;margin-left:auto}.live-header button,.external-resource{white-space:nowrap}.live-header button{padding:6px 9px}.external-resource{display:inline-block;padding:4px 6px;border:1px solid #b7c1cf;border-radius:4px;background:#fff;color:#172033;text-decoration:none;font-size:12px}.external-resource:hover{border-color:#6b8fc7;background:#f1f5f9}.queue-navigation{display:flex;gap:9px;align-items:center;margin-left:auto;font-size:13px}.queue-navigation .nav-disabled{color:#94a3b8}.live-header button.needs-decision,.live-header button.needs-attention{background:#fff4db;border:1px solid #d99b16;border-radius:6px;font-weight:700}.live-workspace iframe{display:block;width:100%;height:calc(100vh - 48px);border:0;transition:width .18s ease}.drawer-open .live-workspace iframe{width:calc(100% - var(--drawer-width))}.assignment-drawer{position:fixed;z-index:20;right:0;top:48px;width:var(--drawer-width);height:calc(100vh - 48px);box-sizing:border-box;overflow:auto;background:#f6f8fb;border-left:1px solid #cbd5e1;box-shadow:-8px 0 20px rgba(15,23,42,.12);padding:14px}.drawer-header,.dialog-header{display:flex;align-items:start;justify-content:space-between;gap:12px}.drawer-header h2,.dialog-header h2{margin:0}.drawer-header button,.dialog-header button{border:0;background:transparent;font-size:28px;line-height:1;cursor:pointer}.preview-panel{margin-top:12px}dialog{width:min(700px,90vw);max-height:88vh;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:10px;padding:18px;color:#172033;background:#f8fafc;box-shadow:0 20px 60px rgba(15,23,42,.3)}dialog::backdrop{background:rgba(15,23,42,.45)}.role-choice{display:grid;grid-template-columns:24px 1fr;column-gap:6px;margin:12px 0;padding:12px;background:white;border:1px solid #dce3ec;border-radius:8px}.role-choice input{grid-row:1/3}.role-choice span{color:#64748b;margin-top:4px}.dialog-actions{display:flex;gap:10px}@media(max-width:1100px){.queue-filters{grid-template-columns:repeat(2,minmax(150px,1fr))}}@media(max-width:800px){.live-review{--drawer-width:min(360px,48vw)}}
.live-review{--drawer-width:min(420px,38vw)}.band-row .eligibility-state{grid-column:1}.band-row .eligibility-toggle{grid-column:2;grid-row:1/3;align-self:center;min-width:124px}.band-row .eligibility-state.pending{color:#9a5b00;font-weight:600}.combined-system-control{display:grid;gap:8px;margin:12px 0;padding:10px;background:#f8fafc;border:1px solid #dce3ec;border-radius:6px}.scope-target-field[hidden],.preview-grid[hidden]{display:none}.scope-target-field select{box-sizing:border-box;width:100%;margin-top:4px}.drawer-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.preview-grid{display:grid;grid-template-columns:1fr;gap:12px;align-items:start;margin-top:12px}.preview-grid .preview-panel{margin-top:0}@media(max-width:800px){.live-review{--drawer-width:min(360px,48vw)}}
.nearby-import-dialog{width:min(900px,94vw)}.nearby-search-controls{display:flex;gap:12px;align-items:end;margin:12px 0}.nearby-search-controls label{display:grid;gap:4px}.nearby-search-controls input{width:90px}.nearby-import-results{max-height:46vh;overflow:auto;border:1px solid #dce3ec;border-radius:7px;margin:10px 0 12px}.nearby-import-results th{position:sticky;top:0;background:#f8fafc;z-index:1}.nearby-import-results td:first-child,.nearby-import-results th:first-child{text-align:center;width:55px}.nearby-import-results td:nth-last-child(2){white-space:nowrap}.import-target-links{display:flex;gap:8px;flex-wrap:wrap}.import-target-links a{display:inline-block;padding:6px 9px;background:white;border:1px solid #b7c1cf;border-radius:5px;text-decoration:none}
@media(max-width:1400px){.live-header{gap:8px}.live-header>span:first-child{flex-shrink:0}.live-header>.muted{display:none}.live-header>strong{max-width:240px;overflow:hidden;text-overflow:ellipsis}.queue-navigation>span:nth-child(2){display:none}.external-resource{padding:3px 4px;font-size:11px}.live-header button{padding:5px 6px;font-size:12px}}
"""


_WORKSPACE_JS = r"""
let currentPayload=null;
let currentPreview=null;
let currentEligibilityPayload=null;
let currentEligibilityPreview=null;
let currentProviderPayload=null;
let currentProviderPreview=null;
let currentCatalogAssociationPayload=null;
let currentCatalogAssociationPreview=null;
const drawer=document.getElementById('assignment-drawer');
const skyReview=document.getElementById('sky-review');
let reviewDrawerVisible=false;
try{
  reviewDrawerVisible=sessionStorage.getItem('sdb-review-tools-visible')==='true';
}catch(error){
  reviewDrawerVisible=false;
}
function pointDisplayId(point){return point.source_display_name||point.source_id;}
function escapeHtml(value){
  return String(value).replaceAll('&','&amp;').replaceAll('<','&lt;')
    .replaceAll('>','&gt;').replaceAll('"','&quot;');
}
function selectedSourceHtml(point,suffix=''){
  const label=escapeHtml(pointDisplayId(point));
  const provenance=(point.provenance||[]).find(
    value=>String(value.access_url||'').startsWith('https://')
  );
  const source=provenance
    ? `<a href="${escapeHtml(provenance.access_url)}" target="_blank" rel="noopener">${label}</a>`
    : label;
  return `${escapeHtml(point.provider)} · ${source} · ${Number(point.separation_arcsec).toFixed(2)} arcsec${escapeHtml(suffix)}`;
}
function pointRunTarget(point){
  if(!point.run_target_sdbid) return '';
  return window.SDB_TARGET_NAMES[point.run_target_sdbid]||point.run_target_sdbid;
}
function postDrawerState(){
  skyReview.contentWindow?.postMessage(
    {type:'sdb-review-drawer-state',visible:reviewDrawerVisible},
    window.location.origin,
  );
}
function syncDrawerVisibility(){
  drawer.hidden=!reviewDrawerVisible;
  document.body.classList.toggle('drawer-open',reviewDrawerVisible);
}
function setDrawerVisibility(visible){
  reviewDrawerVisible=Boolean(visible);
  try{
    sessionStorage.setItem(
      'sdb-review-tools-visible',String(reviewDrawerVisible),
    );
  }catch(error){
    // The toggle still works when browser storage is unavailable.
  }
  syncDrawerVisibility();
  postDrawerState();
}
function clearDrawerSelection(){
  currentPayload=null;
  currentPreview=null;
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  currentProviderPayload=null;
  currentProviderPreview=null;
  currentCatalogAssociationPayload=null;
  currentCatalogAssociationPreview=null;
  document.getElementById('apply').disabled=true;
  document.getElementById('apply-eligibility').disabled=true;
  document.getElementById('apply-provider-result').disabled=true;
  document.getElementById('apply-catalog-association').disabled=true;
  document.querySelectorAll('.detection').forEach(section=>section.classList.remove('active'));
  document.getElementById('detection-editors').hidden=true;
  document.querySelector('.preview-grid').hidden=true;
  document.getElementById('provider-result-editor').hidden=true;
  document.getElementById('provider-result-preview-panel').hidden=true;
  document.getElementById('catalog-association-editor').hidden=true;
  document.getElementById('catalog-association-preview-panel').hidden=true;
  document.getElementById('drawer-title').textContent='Review tools';
  document.getElementById('selected-source').textContent='';
  document.getElementById('assignment-prompt').textContent='Select a plotted catalog source to review it.';
}
function closeDrawer(){
  clearDrawerSelection();
  setDrawerVisibility(false);
}
function showDetection(point,detectionId){
  const section=document.querySelector(`.detection[data-detection="${detectionId}"]`);
  if(!section){clearDrawerSelection();return;}
  document.querySelectorAll('.detection').forEach(value=>value.classList.toggle('active',value===section));
  document.getElementById('detection-editors').hidden=false;
  document.querySelector('.preview-grid').hidden=false;
  document.getElementById('provider-result-editor').hidden=true;
  document.getElementById('provider-result-preview-panel').hidden=true;
  document.getElementById('catalog-association-editor').hidden=true;
  document.getElementById('catalog-association-preview-panel').hidden=true;
  document.getElementById('drawer-title').textContent='Photometry assignment';
  resetEligibilityControls(section);
  const combinedSystem=section.querySelector('.composite-scope');
  if(combinedSystem) updateCombinedSystemControl(combinedSystem);
  document.getElementById('selected-source').innerHTML=selectedSourceHtml(point);
  document.getElementById('assignment-prompt').textContent='Review the selected catalog detection. All bands are selected by default.';
  document.getElementById('preview').textContent='Choose assignments, then preview.';
  document.getElementById('eligibility-preview').textContent='Choose a band action, then preview.';
  document.getElementById('apply').disabled=true;
  document.getElementById('apply-eligibility').disabled=true;
  currentPayload=null;
  currentPreview=null;
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  currentProviderPayload=null;
  currentProviderPreview=null;
  currentCatalogAssociationPayload=null;
  currentCatalogAssociationPreview=null;
}
function showProviderReview(point){
  document.querySelectorAll('.detection').forEach(section=>section.classList.remove('active'));
  document.getElementById('detection-editors').hidden=true;
  document.querySelector('.preview-grid').hidden=true;
  const editor=document.getElementById('provider-result-editor');
  editor.hidden=false;
  editor.classList.add('active');
  document.getElementById('provider-result-preview-panel').hidden=false;
  document.getElementById('catalog-association-editor').hidden=true;
  document.getElementById('catalog-association-preview-panel').hidden=true;
  document.getElementById('drawer-title').textContent='Provider result review';
  const runTarget=pointRunTarget(point);
  document.getElementById('selected-source').innerHTML=selectedSourceHtml(
    point,runTarget?` · catalog query for ${runTarget}`:''
  );
  document.getElementById('assignment-prompt').textContent='Review this catalog provider result.';
  document.getElementById('provider-result-context').textContent=`${point.provider} · ${point.status}${runTarget?` · result belongs to the catalog run for ${runTarget}`:''}${point.note?` · ${point.note}`:''}`;
  for(const button of editor.querySelectorAll('.preview-provider-result')){
    const action=button.dataset.action;
    button.hidden=!((point.status==='ambiguous' && ['accept_candidate','reviewed_no_match'].includes(action))||(['transient_failure','permanent_failure'].includes(point.status)&&action==='retry'));
    button.dataset.runId=point.run_id;
    button.dataset.rawRowId=point.raw_row_id??'';
  }
  document.getElementById('provider-result-preview').textContent='Choose an action, then preview.';
  document.getElementById('apply-provider-result').disabled=true;
  currentProviderPayload=null;
  currentProviderPreview=null;
}
function showCatalogAssociation(point,detectionId){
  document.querySelectorAll('.detection').forEach(section=>section.classList.remove('active'));
  const hasPhotometry=detectionId!=null && point.status==='accepted';
  document.getElementById('detection-editors').hidden=!hasPhotometry;
  document.querySelector('.preview-grid').hidden=!hasPhotometry;
  if(hasPhotometry){
    const section=document.querySelector(`.detection[data-detection="${detectionId}"]`);
    if(section){
      section.classList.add('active');
      resetEligibilityControls(section);
    }
  }
  document.getElementById('provider-result-editor').hidden=true;
  document.getElementById('provider-result-preview-panel').hidden=true;
  const editor=document.getElementById('catalog-association-editor');
  editor.hidden=false;
  editor.classList.add('active');
  document.getElementById('catalog-association-preview-panel').hidden=false;
  document.getElementById('drawer-title').textContent=hasPhotometry?'Source association and photometry':'Catalog source association';
  const runTarget=pointRunTarget(point);
  document.getElementById('selected-source').innerHTML=selectedSourceHtml(point);
  document.getElementById('assignment-prompt').textContent=hasPhotometry
    ? 'This source is accepted for the current target. Its photometry can be assigned below.'
    : 'Decide whether this discovered source belongs to the current target.';
  document.getElementById('catalog-association-context').textContent=`${point.provider} · ${point.status}${runTarget?` · discovered by the catalog query for ${runTarget}`:''}${point.note?` · ${point.note}`:''}`;
  for(const button of editor.querySelectorAll('.preview-catalog-association')){
    button.dataset.detectionId=point.detection_id;
    button.dataset.rawRowId=point.raw_row_id;
  }
  document.getElementById('catalog-association-preview').textContent='Choose an action, then preview.';
  document.getElementById('apply-catalog-association').disabled=true;
  currentCatalogAssociationPayload=null;
  currentCatalogAssociationPreview=null;
}
window.addEventListener('message',event=>{
  if(event.origin!==window.location.origin) return;
  if(event.source!==skyReview.contentWindow) return;
  if(event.data?.type==='sdb-review-drawer-ready'){
    postDrawerState();
    return;
  }
  if(event.data?.type==='sdb-review-drawer-toggle'){
    setDrawerVisibility(event.data.visible);
    return;
  }
  if(event.data?.type==='sdb-review-relatives'){
    openRelativesDialog();
    return;
  }
  if(event.data?.type!=='sdb-review-selection') return;
  const point=event.data.point;
  if(!point){clearDrawerSelection();return;}
  const detectionId=point.raw_row_id==null?null:window.SDB_RAW_ROW_DETECTIONS[String(point.raw_row_id)];
  if(point.kind==='catalog_association'){
    showCatalogAssociation(point,detectionId);
    return;
  }
  if(point.kind==='catalog'&&(point.status==='ambiguous'||['transient_failure','permanent_failure'].includes(point.status))){
    showProviderReview(point);
    return;
  }
  if(detectionId==null){clearDrawerSelection();return;}
  showDetection(point,detectionId);
});
document.getElementById('close-drawer').addEventListener('click',closeDrawer);
clearDrawerSelection();
syncDrawerVisibility();
function payloadFor(section){
  const combinedSystem=section.querySelector('.composite-scope');
  const scopeTarget=section.querySelector('.scope-target');
  return {
    detection_id:Number(section.dataset.detection),
    scope_target:scopeTarget?.value||window.SDB_TARGET,
    contributors:[...section.querySelectorAll('.contributor:checked')].map(x=>x.value),
    include_composite_scope:Boolean(combinedSystem?.checked),
    measurement_ids:[...section.querySelectorAll('.measurement:checked')].map(x=>Number(x.value)),
    target_role:'',
    target_state:'',
  };
}
function eligibilityPayloadFor(section){
  const changes=[...section.querySelectorAll('.eligibility-toggle')]
    .filter(button=>button.dataset.desiredExcluded!==button.dataset.currentExcluded)
    .map(button=>({
      measurement_id:Number(button.dataset.measurement),
      excluded:button.dataset.desiredExcluded==='true',
    }));
  return {changes};
}
function updateCombinedSystemControl(checkbox){
  const field=checkbox.closest('.combined-system-control')?.querySelector('.scope-target-field');
  if(field) field.hidden=!checkbox.checked;
}
function updateEligibilityControl(button){
  const current=button.dataset.currentExcluded==='true';
  const desired=button.dataset.desiredExcluded==='true';
  const changed=current!==desired;
  const state=button.closest('.band-row').querySelector('.eligibility-state');
  state.classList.toggle('pending',changed);
  state.textContent=changed
    ? (desired?'Will be excluded from fit':'Will be included in fit')
    : state.dataset.currentLabel;
  button.textContent=changed
    ? (current?'Keep excluded':'Keep included')
    : (current?'Include in fit':'Exclude from fit');
  button.setAttribute('aria-pressed',String(changed));
}
function resetEligibilityControls(section){
  section.querySelectorAll('.eligibility-toggle').forEach(button=>{
    button.dataset.desiredExcluded=button.dataset.currentExcluded;
    updateEligibilityControl(button);
  });
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
function prefillReason(inputId,preview){
  const input=document.getElementById(inputId);
  if(!input||!preview||!preview.suggested_reason)return;
  if(!input.value||input.value===input.dataset.suggestedReason){
    input.value=preview.suggested_reason;
    input.dataset.suggestedReason=preview.suggested_reason;
  }
}
document.querySelectorAll('.preview').forEach(button=>button.addEventListener('click',async()=>{
  const section=button.closest('.detection');
  currentPayload=payloadFor(section);
  try{
    currentPreview=await request('/api/decision/preview',currentPayload);
    renderHumanSummary(document.getElementById('preview'),currentPreview);
    prefillReason('reason',currentPreview);
    document.getElementById('apply').disabled=!currentPreview.has_changes;
  }catch(error){renderRequestError(document.getElementById('preview'),error);}
}));
document.querySelectorAll('.composite-scope').forEach(checkbox=>checkbox.addEventListener('change',()=>{
  updateCombinedSystemControl(checkbox);
}));
document.querySelectorAll('.contributor,.measurement,.composite-scope,.scope-target').forEach(control=>control.addEventListener('change',()=>{
  currentPayload=null;
  currentPreview=null;
  document.getElementById('preview').textContent='Assignment changed; preview again.';
  document.getElementById('apply').disabled=true;
}));
document.querySelectorAll('.eligibility-toggle').forEach(button=>button.addEventListener('click',()=>{
  const current=button.dataset.currentExcluded==='true';
  const desired=button.dataset.desiredExcluded==='true';
  button.dataset.desiredExcluded=String(desired===current?!current:current);
  updateEligibilityControl(button);
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  document.getElementById('eligibility-preview').textContent='Include/exclude changed; preview again.';
  document.getElementById('apply-eligibility').disabled=true;
}));
document.querySelectorAll('.preview-eligibility').forEach(button=>button.addEventListener('click',async()=>{
  const section=button.closest('.detection');
  currentEligibilityPayload=eligibilityPayloadFor(section);
  if(!currentEligibilityPayload.changes.length){
    currentEligibilityPreview=null;
    document.getElementById('eligibility-preview').textContent='Use Include or Exclude for at least one band.';
    document.getElementById('apply-eligibility').disabled=true;
    return;
  }
  try{
    currentEligibilityPreview=await request('/api/eligibility/preview',currentEligibilityPayload);
    renderHumanSummary(document.getElementById('eligibility-preview'),currentEligibilityPreview);
    prefillReason('reason',currentEligibilityPreview);
    document.getElementById('apply-eligibility').disabled=!currentEligibilityPreview.has_changes;
  }catch(error){renderRequestError(document.getElementById('eligibility-preview'),error);}
}));
document.querySelectorAll('.preview-provider-result').forEach(button=>button.addEventListener('click',async()=>{
  currentProviderPayload={
    action:button.dataset.action,
    run_id:Number(button.dataset.runId),
    raw_row_id:button.dataset.rawRowId===''?null:Number(button.dataset.rawRowId),
  };
  try{
    currentProviderPreview=await request('/api/provider-result/preview',currentProviderPayload);
    renderHumanSummary(document.getElementById('provider-result-preview'),currentProviderPreview);
    prefillReason('reason',currentProviderPreview);
    document.getElementById('apply-provider-result').disabled=!currentProviderPreview.has_changes;
  }catch(error){
    currentProviderPreview=null;
    renderRequestError(document.getElementById('provider-result-preview'),error);
    document.getElementById('apply-provider-result').disabled=true;
  }
}));
document.querySelectorAll('.preview-catalog-association').forEach(button=>button.addEventListener('click',async()=>{
  currentCatalogAssociationPayload={
    target:window.SDB_TARGET,
    action:button.dataset.action,
    detection_id:Number(button.dataset.detectionId),
    raw_row_id:Number(button.dataset.rawRowId),
  };
  try{
    currentCatalogAssociationPreview=await request('/api/catalog-association/preview',currentCatalogAssociationPayload);
    renderHumanSummary(document.getElementById('catalog-association-preview'),currentCatalogAssociationPreview);
    prefillReason('reason',currentCatalogAssociationPreview);
    document.getElementById('apply-catalog-association').disabled=!currentCatalogAssociationPreview.has_changes;
  }catch(error){
    currentCatalogAssociationPreview=null;
    renderRequestError(document.getElementById('catalog-association-preview'),error);
    document.getElementById('apply-catalog-association').disabled=true;
  }
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
  if(!confirm('Apply the displayed fit include/exclude changes?'))return;
  const payload={...currentEligibilityPayload,actor,reason,state_token:currentEligibilityPreview.state_token};
  try{
    const value=await request('/api/eligibility/apply',payload);
    renderHumanSummary(document.getElementById('eligibility-preview'),value);
    document.getElementById('apply-eligibility').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('eligibility-preview'),error);}
});
document.getElementById('apply-provider-result').addEventListener('click',async()=>{
  if(!currentProviderPayload||!currentProviderPreview)return;
  const actor=document.getElementById('actor').value;
  const reason=document.getElementById('reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  if(!confirm('Apply the displayed provider result action?'))return;
  const payload={...currentProviderPayload,actor,reason,state_token:currentProviderPreview.state_token};
  try{
    const value=await request('/api/provider-result/apply',payload);
    renderHumanSummary(document.getElementById('provider-result-preview'),value);
    document.getElementById('apply-provider-result').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('provider-result-preview'),error);}
});
document.getElementById('apply-catalog-association').addEventListener('click',async()=>{
  if(!currentCatalogAssociationPayload||!currentCatalogAssociationPreview)return;
  const actor=document.getElementById('actor').value;
  const reason=document.getElementById('reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  if(!confirm('Apply the displayed catalog source association?'))return;
  const payload={...currentCatalogAssociationPayload,actor,reason,state_token:currentCatalogAssociationPreview.state_token};
  try{
    const value=await request('/api/catalog-association/apply',payload);
    renderHumanSummary(document.getElementById('catalog-association-preview'),value);
    document.getElementById('apply-catalog-association').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('catalog-association-preview'),error);}
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
    prefillReason('lifecycle-reason',lifecyclePreview);
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
    prefillReason('relatives-reason',relativesPreview);
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
const nearbyImportDialog=document.getElementById('nearby-import-dialog');
let nearbyImportSearch=null;
function updateNearbyImportButton(){
  document.getElementById('apply-nearby-import').disabled=
    document.querySelectorAll('#nearby-import-rows input:checked').length===0;
}
function renderNearbyImportRows(value){
  const body=document.getElementById('nearby-import-rows');
  body.innerHTML=value.candidates.map(row=>{
    const params=new URLSearchParams({submit:'submit id',Ident:row.main_id});
    const simbadUrl=`https://simbad.cds.unistra.fr/simbad/sim-id?${params}`;
    const objectType=row.object_type_description||row.object_type_label||
      row.primary_object_type||
      (row.object_types||[]).join(', ')||'—';
    let status='New';
    if(row.current_target){
      status='Current target';
    }else if(row.existing_sdbid){
      status=`<a href="/target/${encodeURIComponent(row.existing_sdbid)}">${escapeHtml(row.existing_sdbid)}</a>`;
    }else if(row.blocked_reason){
      status=`Context only · ${escapeHtml(row.blocked_reason)}`;
    }
    const selection=row.selectable
      ? `<input type="checkbox" value="${escapeHtml(row.main_id)}" aria-label="Import ${escapeHtml(row.main_id)}">`
      : '—';
    return `<tr><td>${selection}</td>`+
      `<td><a href="${escapeHtml(simbadUrl)}" target="_blank" rel="noopener">${escapeHtml(row.main_id)}</a></td>`+
      `<td>${escapeHtml(objectType)}</td>`+
      `<td>${escapeHtml(row.spectral_type||'—')}</td>`+
      `<td>${Number(row.separation_arcsec).toFixed(2)}″</td>`+
      `<td>${status}</td></tr>`;
  }).join('');
  document.getElementById('nearby-import-results').hidden=false;
  body.querySelectorAll('input').forEach(
    input=>input.addEventListener('change',updateNearbyImportButton)
  );
  updateNearbyImportButton();
}
async function searchNearbyImport(){
  const radius=Number(document.getElementById('nearby-import-radius').value);
  const status=document.getElementById('nearby-import-search-status');
  const button=document.getElementById('search-nearby-import');
  if(!Number.isFinite(radius)||radius<=0||radius>600){
    alert('Radius must be between 1 and 600 arcsec.');
    return;
  }
  button.disabled=true;
  button.textContent='Searching…';
  document.getElementById('apply-nearby-import').disabled=true;
  document.getElementById('nearby-import-results').hidden=true;
  document.getElementById('nearby-import-summary').textContent='';
  document.getElementById('nearby-import-target-links').innerHTML='';
  status.classList.add('muted');
  status.textContent='Searching SIMBAD around the target position…';
  try{
    nearbyImportSearch=await request('/api/nearby-import/search',{
      target:window.SDB_TARGET,
      radius_arcsec:radius,
    });
    renderNearbyImportRows(nearbyImportSearch);
    status.textContent=`${nearbyImportSearch.candidates.length} object(s), ${nearbyImportSearch.new_count} available to import, ${nearbyImportSearch.blocked_count} context only; sorted by distance.`;
  }catch(error){
    nearbyImportSearch=null;
    renderRequestError(status,error);
  }finally{
    button.disabled=false;
    button.textContent='Search SIMBAD';
  }
}
document.getElementById('nearby-import').addEventListener('click',()=>{
  if(!nearbyImportDialog.open)nearbyImportDialog.showModal();
  if(!nearbyImportSearch)searchNearbyImport();
});
document.getElementById('search-nearby-import').addEventListener('click',searchNearbyImport);
document.getElementById('apply-nearby-import').addEventListener('click',async()=>{
  const selected=[
    ...document.querySelectorAll('#nearby-import-rows input:checked')
  ].map(input=>input.value);
  if(!selected.length)return;
  if(!confirm(`Import ${selected.length} selected SIMBAD object(s) and fill provider coverage?`))return;
  const button=document.getElementById('apply-nearby-import');
  const summary=document.getElementById('nearby-import-summary');
  button.disabled=true;
  button.textContent='Importing and updating…';
  try{
    const value=await request('/api/nearby-import/apply',{
      target:window.SDB_TARGET,
      main_ids:selected,
    });
    renderHumanSummary(summary,value);
    const links=value.items.filter(item=>item.sdbid).map(item=>
      `<a href="/target/${encodeURIComponent(item.sdbid)}">Open ${escapeHtml(item.requested_name)}</a>`
    );
    document.getElementById('nearby-import-target-links').innerHTML=links.join('');
    for(const input of document.querySelectorAll('#nearby-import-rows input:checked')){
      input.checked=false;
      input.disabled=true;
      input.closest('tr').lastElementChild.textContent='Imported';
    }
  }catch(error){
    renderRequestError(summary,error);
    button.disabled=false;
  }finally{
    button.textContent='Import selected';
    updateNearbyImportButton();
  }
});
const catalogCoverageDialog=document.getElementById('catalog-coverage-dialog');
let catalogCoveragePreview=null;
async function refreshCatalogCoveragePreview(){
  const element=document.getElementById('catalog-coverage-preview');
  const applyButton=document.getElementById('apply-catalog-coverage');
  element.classList.add('muted');
  element.textContent='Checking direct provider coverage…';
  applyButton.disabled=true;
  try{
    catalogCoveragePreview=await request('/api/catalog-coverage/preview',{target:window.SDB_TARGET});
    renderHumanSummary(element,catalogCoveragePreview);
    applyButton.disabled=!catalogCoveragePreview.has_changes||!catalogCoveragePreview.action_available;
  }catch(error){
    catalogCoveragePreview=null;
    renderRequestError(element,error);
  }
}
document.getElementById('catalog-coverage').addEventListener('click',()=>{
  if(!catalogCoverageDialog.open)catalogCoverageDialog.showModal();
  refreshCatalogCoveragePreview();
});
document.getElementById('preview-catalog-coverage').addEventListener('click',refreshCatalogCoveragePreview);
document.getElementById('apply-catalog-coverage').addEventListener('click',async()=>{
  if(!catalogCoveragePreview)return;
  if(!confirm('Complete the displayed catalog normalization and provider gaps?'))return;
  const button=document.getElementById('apply-catalog-coverage');
  button.disabled=true;
  button.textContent='Updating…';
  try{
    const value=await request('/api/catalog-coverage/apply',{
      target:window.SDB_TARGET,
      state_token:catalogCoveragePreview.state_token,
    });
    catalogCoveragePreview=value;
    renderHumanSummary(document.getElementById('catalog-coverage-preview'),value);
    setTimeout(()=>location.reload(),1000);
  }catch(error){
    renderRequestError(document.getElementById('catalog-coverage-preview'),error);
    button.disabled=false;
  }finally{
    button.textContent='Complete catalog gaps';
  }
});
"""
