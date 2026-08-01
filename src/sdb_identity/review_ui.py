from __future__ import annotations

import html
import hashlib
import json
import math
import os
import threading
import webbrowser
from collections import defaultdict
from importlib.resources import files
from typing import Callable
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .decisions import DecisionContext
from .hierarchy_system_context import HierarchySystemContextService
from .models import CatalogRun, RawCatalogRow, Target
from .review_commands import (
    review_catalog_association_command,
    review_detection_command,
    review_eligibility_command,
    review_lifecycle_command,
    review_provider_result_command,
)
from .review_dashboard import review_dashboard_report
from .review_workspace import (
    TargetWorkspace,
    build_target_workspace,
    filtered_queue_rows as _filtered_queue_rows,
    queue_filters as _queue_filters,
    queue_query as _queue_query,
)
from .review_sky_render import render_review_sky_html
from .review_widget import build_review_sky_view
from .service import IdentityService
from .system_expansion import (
    import_immediate_relatives,
    preview_immediate_relatives,
)
from .target_import import (
    TargetImportService,
    search_nearby_simbad,
)


def _review_asset(name: str) -> str:
    return files("sdb_identity.review_assets").joinpath(name).read_text(
        encoding="utf-8"
    )


_CSS = _review_asset("review.css")
_WORKSPACE_JS = _review_asset("workspace.js")


def create_review_app(
    session_factory: sessionmaker[Session], *, sample: str | None = None,
    identity_service_factory: Callable[[], IdentityService] | None = None,
    catalog_service_factory: Callable[[str, str], object] | None = None,
    catalog_coverage_providers: tuple[str, ...] | None = None,
    catalog_update_factory: Callable[[], object] | None = None,
    reference_store: object | None = None,
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
            workspace = build_target_workspace(
                session_factory,
                sdbid,
                sample=sample,
                filters=filters,
                position=position,
                catalog_coverage_providers=catalog_coverage_providers,
                catalog_update_available=catalog_update_factory is not None,
                nearby_import_available=(
                    identity_service_factory is not None
                    and catalog_update_factory is not None
                ),
            )
            return _target_page(workspace)
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

    @app.get("/catalogs", response_class=HTMLResponse)
    def catalogs_page():
        from .catalog_overview import catalog_overview

        return _catalogs_page(catalog_overview(reference_store))

    @app.get("/api/catalogs")
    def catalogs_api():
        from .catalog_overview import catalog_overview

        return catalog_overview(reference_store)

    @app.get("/api/target/{sdbid}")
    def target_api(
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
            return build_target_workspace(
                session_factory,
                sdbid,
                sample=sample,
                filters=_queue_filters(
                    view=view,
                    priority=priority,
                    role=role,
                    classification=classification,
                    provider=provider,
                    search=search,
                ),
                position=position,
                catalog_coverage_providers=catalog_coverage_providers,
                catalog_update_available=catalog_update_factory is not None,
                nearby_import_available=(
                    identity_service_factory is not None
                    and catalog_update_factory is not None
                ),
            ).as_dict()
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/decision/preview")
    async def preview(payload: dict[str, object]):
        try:
            return review_detection_command(
                session_factory, payload, apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/decision/apply")
    async def apply(payload: dict[str, object]):
        try:
            return review_detection_command(
                session_factory, payload, apply=True,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/lifecycle/preview")
    async def lifecycle_preview(payload: dict[str, object]):
        try:
            return review_lifecycle_command(
                session_factory, payload, apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/lifecycle/apply")
    async def lifecycle_apply(payload: dict[str, object]):
        try:
            return review_lifecycle_command(
                session_factory, payload, apply=True,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/eligibility/preview")
    async def eligibility_preview(payload: dict[str, object]):
        try:
            return review_eligibility_command(
                session_factory, payload, apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/eligibility/apply")
    async def eligibility_apply(payload: dict[str, object]):
        try:
            return review_eligibility_command(
                session_factory, payload, apply=True,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/provider-result/preview")
    async def provider_result_preview(payload: dict[str, object]):
        try:
            return review_provider_result_command(
                session_factory,
                catalog_service_factory,
                payload,
                apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/provider-result/apply")
    async def provider_result_apply(payload: dict[str, object]):
        try:
            return review_provider_result_command(
                session_factory,
                catalog_service_factory,
                payload,
                apply=True,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/catalog-association/preview")
    async def catalog_association_preview(payload: dict[str, object]):
        try:
            return review_catalog_association_command(
                session_factory, payload, apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/catalog-association/apply")
    async def catalog_association_apply(payload: dict[str, object]):
        try:
            return review_catalog_association_command(
                session_factory, payload, apply=True,
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
    reference_store: object | None = None,
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
        reference_store=reference_store,
    )
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


def _with_human_summary(
    value: dict[str, object], summary: dict[str, object],
) -> dict[str, object]:
    return {**value, "human_summary": summary}


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
    context = HierarchySystemContextService(session_factory).system_context(
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
  <h1>SDB sample review <a class="section-link" href="/catalogs">Catalogs</a></h1>
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


def _catalogs_page(report: dict[str, object]) -> str:
    rows = []
    for provider in report["providers"]:
        bands = ", ".join(
            f"{band['name']} ({band['wavelength_micron']:g} µm)"
            for band in provider["bands"]
        ) or "—"
        science_tables = ", ".join(provider["science_tables"]) or "—"
        retained_tables = ", ".join(provider["retained_tables"]) or "—"
        snapshot = provider.get("snapshot")
        snapshot_detail = ""
        if snapshot:
            table_rows = "".join(
                f"<li><code>{_e(table['name'])}</code>: "
                f"{table['row_count']:,} rows"
                f"{' (science)' if table['science'] else ' (retained only)'}</li>"
                for table in snapshot["tables"]
            )
            snapshot_detail = (
                f"<p><strong>Snapshot:</strong> {snapshot['row_count']:,} rows; "
                f"retrieved {_e(snapshot['retrieved_at'])}; "
                f"SHA-256 <code>{_e(snapshot['content_sha256'])}</code></p>"
                f"<ul>{table_rows}</ul>"
            )
        caveats = "".join(
            f"<li>{_e(value)}</li>" for value in provider["caveats"]
        )
        details = f"""
<div class="catalog-detail">
  <p><strong>Science tables:</strong> {_e(science_tables)}<br>
  <strong>Retained-only tables:</strong> {_e(retained_tables)}<br>
  <strong>Identifier policy:</strong> {_e(provider['identifier_policy'])}<br>
  <strong>Component policy:</strong> {_e(provider['component_policy'])}<br>
  <strong>Epoch:</strong> {_e(provider['query_epoch'] if provider['query_epoch'] is not None else 'source identifier')} ·
  <strong>query radius:</strong> {_e(str(provider['radius_arcsec']) + ' arcsec' if provider['radius_arcsec'] is not None else 'n/a')} ·
  <strong>review radius:</strong> {_e(str(provider['review_radius_arcsec']) + ' arcsec' if provider['review_radius_arcsec'] is not None else 'n/a')}<br>
  <strong>Bibliography:</strong> <code>{_e(provider['bibliography'] or '—')}</code></p>
  {snapshot_detail}
  {f'<ul class="warning-list">{caveats}</ul>' if caveats else ''}
</div>"""
        rows.append(
            f"<tr><td><details><summary><strong>{_e(provider['display_name'])}</strong> "
            f"<code>{_e(provider['key'])}</code></summary>{details}</details></td>"
            f"<td><a href='{_e(provider['vizier_url'])}' target='_blank' rel='noopener'>"
            f"<code>{_e(provider['catalog'])}</code></a></td>"
            f"<td>{_e(str(provider['acquisition_mode']).replace('_', ' '))}</td>"
            f"<td>{_e(bands)}</td><td class='catalog-status-{_e(provider['status'])}'>"
            f"{_e(provider['status'])}</td></tr>"
        )
    body = f"""
<main>
  <p><a href="/">← readiness queue</a></p>
  <h1>Catalog providers</h1>
  <p class="muted">One operational view of remote catalogs and locally retained reference snapshots. Expand a provider for matching, component, provenance, and snapshot details.</p>
  <div class="summary">
    <span><strong>{report['provider_count']}</strong> providers</span>
    <span><strong>{report['remote_count']}</strong> remote</span>
    <span><strong>{report['snapshot_current_count']}</strong> snapshots current</span>
    <span><strong>{report['snapshot_missing_count']}</strong> snapshots missing</span>
  </div>
  <table class="catalog-overview"><thead><tr><th>provider</th><th>catalog / release</th><th>acquisition</th><th>products / bands</th><th>status</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table>
</main>"""
    return _page("SDB catalog providers", body)


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


def _target_page(workspace: TargetWorkspace) -> str:
    sdbid = workspace.sdbid
    readiness = workspace.readiness
    graph = workspace.fitting_graph
    raw_row_detections = workspace.raw_row_detections
    navigation = workspace.navigation
    display_name = workspace.display_name
    simbad_main_ids = workspace.simbad_main_ids
    catalog_coverage = list(workspace.catalog_coverage)
    catalog_update_available = workspace.catalog_update_available
    nearby_import_available = workspace.nearby_import_available
    target_position = workspace.target_position
    default_actor = os.environ.get("SDB_ACTOR", "").strip()
    target = next(
        (row for row in graph["targets"] if row["sdbid"] == sdbid),
        None,
    )
    if target is None:
        raise KeyError(f"target is not present in its fitting graph: {sdbid}")
    targets = sorted(graph["targets"], key=lambda row: str(row["sdbid"]))

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
    <span><a href="/catalogs">Catalogs</a></span>
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
