from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .assignment_readiness import assignment_readiness_report
from .fitting_groups import fitting_group_report
from .models.identity import ExternalIdentifier
from .vocabulary import INACTIVE_TARGET_STATES, ReviewPriority, review_priority_rank


def review_dashboard_report(
    session_factory: sessionmaker[Session], *, sample: str,
) -> dict[str, object]:
    """Summarize every selected sample target from current stored review state.

    This deliberately avoids recomputing the expensive positional/identifier
    proposal engine for the full sample. The target workspace still computes
    that richer evidence for one system when it is opened.
    """
    graph = fitting_group_report(session_factory, sample=sample)
    scope_report = assignment_readiness_report(
        session_factory, sample=sample, graph=graph,
    )
    scope_by_target = {
        int(row["target_id"]): row for row in scope_report["rows"]
    }
    selected_targets = {
        int(row["target_id"]): row
        for row in graph["targets"]
        if row["selected"]
    }
    display_names = _target_display_names(
        session_factory, set(selected_targets),
    )
    measurements_by_target: dict[int, dict[int, dict[str, object]]] = defaultdict(dict)
    for measurement in graph["measurements"]:
        relevant_target_ids = set(measurement.get("encounter_target_ids") or [])
        relevant_target_ids.update(
            int(row["target_id"])
            for row in measurement.get("assignments") or []
        )
        if measurement.get("origin_target_id") is not None:
            relevant_target_ids.add(int(measurement["origin_target_id"]))
        for target_id in relevant_target_ids & set(selected_targets):
            measurements_by_target[target_id][int(measurement["measurement_id"])] = (
                measurement
            )

    rows = []
    for target_id, target in selected_targets.items():
        measurements = list(measurements_by_target.get(target_id, {}).values())
        detections = _detection_rows(measurements)
        scope = scope_by_target.get(target_id)
        classification, priority, action = _target_classification(
            target, detections, scope,
        )
        providers = _provider_summary(measurements)
        rows.append({
            "target_id": target_id,
            "sdbid": target["sdbid"],
            "display_name": display_names.get(target_id),
            "role": target["role"],
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
            "detection_count": len(detections),
            "assigned_detection_count": sum(
                row["assignment_state"] == "assigned" for row in detections
            ),
            "unassigned_detection_count": sum(
                row["assignment_state"] == "unassigned" for row in detections
            ),
            "mixed_detection_count": sum(
                row["assignment_state"] == "mixed_band_ownership"
                for row in detections
            ),
            "providers": providers,
            "systems": target["systems"],
            "detections": detections,
            "importable_relative_count": (
                0 if scope is None else scope["importable_relative_count"]
            ),
            "scope_classification": (
                None if scope is None else scope["classification"]
            ),
        })

    rows.sort(key=lambda row: (
        -review_priority_rank(str(row["priority"])), str(row["sdbid"]),
    ))
    return {
        "selection": graph["selection"],
        "summary": {
            "target_count": len(rows),
            "actionable_target_count": sum(
                row["priority"] != ReviewPriority.NONE for row in rows
            ),
            "clean_target_count": sum(
                row["priority"] == ReviewPriority.NONE for row in rows
            ),
            "scope_blocker_target_count": len(scope_by_target),
            "mixed_ownership_target_count": sum(
                row["mixed_detection_count"] > 0 for row in rows
            ),
            "unassigned_target_count": sum(
                row["unassigned_detection_count"] > 0 for row in rows
            ),
            "no_photometry_target_count": sum(
                row["detection_count"] == 0 for row in rows
            ),
            "detection_count": sum(row["detection_count"] for row in rows),
            "unassigned_detection_count": sum(
                row["unassigned_detection_count"] for row in rows
            ),
            "mixed_detection_count": sum(
                row["mixed_detection_count"] for row in rows
            ),
        },
        "rows": rows,
        "notes": [
            "all current sample members are listed, including clean and no-photometry targets",
            "dashboard states use accepted source associations and explicit attribution exceptions",
            "open a target to compute detailed identifier, position, hierarchy, and resolution proposals",
        ],
    }


def _target_display_names(
    session_factory: sessionmaker[Session], target_ids: set[int],
) -> dict[int, str]:
    """Choose a recognizable operator label without changing target identity."""
    if not target_ids:
        return {}
    source_priority = {
        "submitted": 0,
        "simbad_metadata": 1,
        "simbad": 2,
        "gaia_dr3": 3,
        "2mass": 4,
    }
    choices: dict[int, tuple[int, int, str]] = {}
    with session_factory() as session:
        identifiers = session.scalars(
            select(ExternalIdentifier)
            .where(
                ExternalIdentifier.target_id.in_(target_ids),
                ExternalIdentifier.source != "sdb",
            )
            .order_by(ExternalIdentifier.id)
        )
        for identifier in identifiers:
            choice = (
                source_priority.get(identifier.source, 9),
                identifier.id,
                identifier.value,
            )
            current = choices.get(identifier.target_id)
            if current is None or choice < current:
                choices[identifier.target_id] = choice
    return {
        target_id: choice[2] for target_id, choice in choices.items()
    }


def _detection_rows(
    measurements: list[dict[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for measurement in measurements:
        grouped[int(measurement["detection_id"])].append(measurement)
    result = []
    for detection_id, rows in grouped.items():
        signatures = {
            tuple(sorted(
                (int(value["target_id"]), str(value["role"]))
                for value in row.get("assignments") or []
            ))
            for row in rows
        }
        assigned = [bool(signature) for signature in signatures]
        if signatures == {()}:
            assignment_state = "unassigned"
        elif len(signatures) == 1 and all(assigned):
            assignment_state = "assigned"
        else:
            assignment_state = "mixed_band_ownership"
        first = rows[0]
        result.append({
            "detection_id": detection_id,
            "provider": first["provider"],
            "source_id": first["source_id"],
            "bands": sorted({str(row["band"]) for row in rows}),
            "measurement_ids": sorted(int(row["measurement_id"]) for row in rows),
            "assignment_state": assignment_state,
            "included_band_count": sum(not row["fit_excluded"] for row in rows),
            "excluded_band_count": sum(row["fit_excluded"] for row in rows),
        })
    return sorted(result, key=lambda row: (
        str(row["provider"]), str(row["source_id"]), int(row["detection_id"]),
    ))


def _target_classification(
    target: dict[str, object],
    detections: list[dict[str, object]],
    scope: dict[str, object] | None,
) -> tuple[str, str, str]:
    if scope is not None:
        return (
            str(scope["classification"]),
            str(scope["priority"]),
            str(scope["recommended_action"]),
        )
    if any(row["assignment_state"] == "mixed_band_ownership" for row in detections):
        return (
            "mixed_band_ownership", "highest",
            "review one canonical detection whose bands have different ownership",
        )
    unassigned = [
        row for row in detections if row["assignment_state"] == "unassigned"
    ]
    if any(row["included_band_count"] for row in unassigned):
        return (
            "unassigned_photometry", "high",
            "inspect positional/identifier proposals and assign the detection",
        )
    if unassigned:
        return (
            "unassigned_excluded_photometry", "low",
            "confirm ownership for excluded contextual photometry",
        )
    if not detections:
        return (
            "no_current_photometry", "medium",
            "refresh providers or confirm that no catalog photometry is expected",
        )
    if target["state"] in INACTIVE_TARGET_STATES:
        return (
            "inactive_target", "none", "no action; target is not active",
        )
    if any(row["excluded_band_count"] for row in detections):
        return (
            "assigned_with_exclusions", "none",
            "no ownership action; exclusions remain visible for optional review",
        )
    return "assigned_clean", "none", "no ownership action required"


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
            "detection_count": len({int(row["detection_id"]) for row in rows}),
            "bands": sorted({str(row["band"]) for row in rows}),
        })
    return result
