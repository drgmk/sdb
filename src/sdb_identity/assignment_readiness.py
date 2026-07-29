from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy.orm import Session, sessionmaker

from .fitting_groups import fitting_group_report
from .system_expansion import preview_immediate_relatives


_SCOPE_FLAGS = {
    "composite_scope_without_physical_contributor",
    "scope_assignment_requires_target_role_review",
    "physical_target_assigned_as_composite_scope",
}


def assignment_readiness_report(
    session_factory: sessionmaker[Session],
    *,
    target_reference: str | int | None = None,
    sample: str | None = None,
    graph: dict[str, object] | None = None,
) -> dict[str, object]:
    """Group accepted scope-assignment blockers into target-level review rows."""
    if graph is None:
        graph = fitting_group_report(
            session_factory,
            target_reference=target_reference,
            sample=sample,
        )
    targets = {row["target_id"]: row for row in graph["targets"]}
    physical_by_system = _physical_targets_by_system(graph["targets"])
    measurements_by_scope: dict[int, list[dict[str, object]]] = defaultdict(list)
    for measurement in graph["measurements"]:
        if not (_SCOPE_FLAGS & set(measurement["review_flags"])):
            continue
        for target_id in measurement["composite_scope_target_ids"]:
            measurements_by_scope[target_id].append(measurement)

    rows = []
    for target_id, measurements in measurements_by_scope.items():
        target = targets.get(target_id)
        if target is None:
            continue
        relatives, relative_error = _relative_preview(
            session_factory, target["sdbid"]
        )
        relative_counts = Counter(
            str(relative["action"]) for relative in relatives
        )
        already_imported_relative_count = (
            relative_counts["reconcile"] + relative_counts["complete"]
        )
        imported_physical = _imported_physical_relatives(
            target, physical_by_system,
        )
        providers = _provider_summary(measurements)
        role = str(target["role"])
        classification, priority, action = _classification(
            role,
            imported_physical_count=len(imported_physical),
            importable_relative_count=relative_counts["import"],
            already_imported_relative_count=already_imported_relative_count,
        )
        rows.append({
            "target_id": target_id,
            "sdbid": target["sdbid"],
            "role": role,
            "state": target["state"],
            "classification": classification,
            "priority": priority,
            "recommended_action": action,
            "measurement_count": len(measurements),
            "included_measurement_count": sum(
                not row["fit_excluded"] for row in measurements
            ),
            "excluded_measurement_count": sum(
                row["fit_excluded"] for row in measurements
            ),
            "detection_count": len({
                row["detection_id"] for row in measurements
            }),
            "providers": providers,
            "systems": target["systems"],
            "imported_physical_relatives": imported_physical,
            "importable_relative_count": relative_counts["import"],
            "already_imported_relative_count": already_imported_relative_count,
            "reconcilable_relative_count": relative_counts["reconcile"],
            "reconciled_relative_count": relative_counts["complete"],
            "context_only_relative_count": relative_counts["context_only"],
            "relative_review_required_count": relative_counts["review_required"],
            "relative_preview_error": relative_error,
            "available_relatives": relatives,
            "measurement_ids": sorted(row["measurement_id"] for row in measurements),
        })
    priority_order = {"highest": 0, "high": 1, "medium": 2, "low": 3}
    rows.sort(key=lambda row: (
        priority_order.get(str(row["priority"]), 9), str(row["sdbid"]),
    ))

    unassigned = [
        row for row in graph["measurements"] if not row["assignments"]
    ]
    return {
        "selection": graph["selection"],
        "summary": {
            "scope_target_count": len(rows),
            "confirmed_composite_target_count": sum(
                row["role"] == "composite" for row in rows
            ),
            "unspecified_role_target_count": sum(
                row["role"] == "unspecified" for row in rows
            ),
            "physical_scope_error_target_count": sum(
                row["role"] == "physical" for row in rows
            ),
            "scope_blocker_measurement_count": len({
                measurement_id
                for row in rows for measurement_id in row["measurement_ids"]
            }),
            "importable_relative_count": sum(
                row["importable_relative_count"] for row in rows
            ),
            "already_imported_relative_count": sum(
                row["already_imported_relative_count"] for row in rows
            ),
            "unassigned_measurement_count": len(unassigned),
            "unassigned_included_measurement_count": sum(
                not row["fit_excluded"] for row in unassigned
            ),
            "unassigned_by_provider": _count_by_provider(unassigned),
        },
        "rows": rows,
        "notes": [
            "rows are grouped by accepted composite-scope target rather than catalog band",
            "unspecified target roles require classification before component import or fitting",
            "SIMBAD relatives are previewed only; this command never imports targets or changes assignments",
        ],
    }


def _physical_targets_by_system(
    targets: list[dict[str, object]],
) -> dict[int, list[dict[str, object]]]:
    result: dict[int, list[dict[str, object]]] = defaultdict(list)
    for target in targets:
        if target["role"] != "physical" or not target["model_target"]:
            continue
        for system in target["systems"]:
            result[system["system_id"]].append({
                "target_id": target["target_id"],
                "sdbid": target["sdbid"],
                "component_label": system["component_label"],
            })
    return result


def _imported_physical_relatives(
    target: dict[str, object],
    physical_by_system: dict[int, list[dict[str, object]]],
) -> list[dict[str, object]]:
    values = {}
    for system in target["systems"]:
        for relative in physical_by_system.get(system["system_id"], []):
            if relative["target_id"] == target["target_id"]:
                continue
            values[relative["target_id"]] = relative
    return sorted(values.values(), key=lambda row: str(row["sdbid"]))


def _relative_preview(
    session_factory: sessionmaker[Session], sdbid: str,
) -> tuple[list[dict[str, object]], str | None]:
    try:
        return preview_immediate_relatives(session_factory, sdbid), None
    except (KeyError, ValueError) as error:
        return [], str(error)


def _provider_summary(
    measurements: list[dict[str, object]],
) -> list[dict[str, object]]:
    result = []
    for provider in sorted({str(row["provider"]) for row in measurements}):
        rows = [row for row in measurements if row["provider"] == provider]
        result.append({
            "provider": provider,
            "measurement_count": len(rows),
            "included_count": sum(not row["fit_excluded"] for row in rows),
            "detection_count": len({row["source_id"] for row in rows}),
            "bands": sorted({str(row["band"]) for row in rows}),
        })
    return result


def _classification(
    role: str,
    *,
    imported_physical_count: int,
    importable_relative_count: int,
    already_imported_relative_count: int,
) -> tuple[str, str, str]:
    if role == "composite":
        if imported_physical_count or already_imported_relative_count:
            return (
                "confirmed_composite_missing_contributors", "highest",
                "assign imported physical relatives as contributors",
            )
        if importable_relative_count:
            return (
                "confirmed_composite_missing_targets", "highest",
                "preview and import immediate stellar relatives",
            )
        return (
            "confirmed_composite_contributors_unknown", "highest",
            "review hierarchy and identify physical contributors",
        )
    if role == "physical":
        return (
            "physical_target_used_as_composite_scope", "highest",
            "correct the scope assignment or revise target role",
        )
    return (
        "target_role_unspecified", "high",
        "decide whether the target is physical or composite",
    )


def _count_by_provider(
    measurements: list[dict[str, object]],
) -> list[dict[str, object]]:
    counts = Counter(str(row["provider"]) for row in measurements)
    return [
        {"provider": provider, "count": count}
        for provider, count in sorted(counts.items())
    ]
