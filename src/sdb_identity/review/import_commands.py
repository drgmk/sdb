"""Transport-independent import and catalog-coverage review commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from ..decisions import DecisionContext
from ..hierarchy.system_context import HierarchySystemContextService
from ..service import IdentityService
from ..hierarchy.expansion import (
    import_immediate_relatives,
    preview_immediate_relatives,
)
from ..target_import import TargetImportService, search_nearby_simbad


def _with_human_summary(
    value: dict[str, object], summary: dict[str, object],
) -> dict[str, object]:
    return {**value, "human_summary": summary}


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def review_relatives_command(
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


def search_nearby_import_command(
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


def apply_nearby_import_command(
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


def review_catalog_coverage_command(
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
