"""Hierarchy-informed target assignment and photometry review projections."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .catalogs.measurements import current_measurements_for_target
from .hierarchy_semantics import normalize_component_label
from .models.identity import Target
from .vocabulary import ReviewPriority


def target_component_assignment(
    *,
    semantic_identity: dict[str, object],
    nearest_component: dict[str, object] | None,
    closest_companion: dict[str, object] | None,
    systems: list[dict[str, object]],
    review_required: bool,
) -> dict[str, object]:
    semantic_kind = str(semantic_identity["kind"])
    semantic_component = _best_component_label_candidate(
        semantic_identity.get("component_label_candidates") or [],
    )
    relevance_counts = semantic_identity.get("relationship_relevance_counts") or {}
    stellar_relationships = int(
        relevance_counts.get("stellar_or_substellar_component", 0),
    )
    nonstellar_relationships = (
        int(relevance_counts.get("planetary_or_disk", 0))
        + int(relevance_counts.get("contextual_group", 0))
    )
    nearest_label = None if nearest_component is None else nearest_component["component"]
    nearest_sep = (
        None if nearest_component is None else nearest_component["separation_arcsec"]
    )
    disconnected_groups = [
        system for system in systems
        for diagnostic in system["diagnostics"]
        if diagnostic["issue"] == "disconnected_structural_groups"
    ]
    if review_required:
        status, confidence = "review_required", "low"
        reason = "review-level hierarchy diagnostics are present"
    elif nearest_component is None:
        status = "semantic_only" if semantic_kind != "unknown" else "no_assignment"
        confidence = "low" if semantic_kind == "unknown" else "medium"
        reason = "no provider component geometry is available"
    elif (
        semantic_component
        and nearest_label
        and _component_labels_match(semantic_component, str(nearest_label))
    ):
        status, confidence = "semantic_geometry_agree", "high"
        reason = "SIMBAD component label candidate agrees with nearest provider component"
    elif (
        semantic_component
        and nearest_label
        and _component_label_contains(semantic_component, str(nearest_label))
    ):
        status, confidence = "semantic_group_contains_nearest_component", "medium"
        reason = (
            "SIMBAD component label candidate is a group containing the nearest "
            "provider component"
        )
    elif semantic_component and nearest_label:
        status, confidence = "semantic_geometry_conflict", "low"
        reason = "SIMBAD component label candidate conflicts with nearest provider component"
    elif disconnected_groups and semantic_kind in {"system_or_parent", "subsystem"}:
        status, confidence = "ambiguous_disconnected_groups", "low"
        reason = (
            "provider geometry has disconnected top-level groups; do not assume one "
            "parent system without semantic support"
        )
    elif semantic_kind == "unknown":
        status, confidence = "geometry_only", "medium"
        reason = "nearest provider component is based on geometry only"
    elif stellar_relationships == 0 and nonstellar_relationships > 0:
        status, confidence = "semantic_hierarchy_not_stellar_component", "medium"
        reason = (
            "SIMBAD hierarchy relationships are non-stellar/contextual for "
            "component-blending purposes"
        )
    elif semantic_kind == "single_or_no_known_hierarchy":
        status, confidence = "geometry_has_hierarchy_but_simbad_does_not", "medium"
        reason = (
            "SIMBAD has no hierarchy relationships, but provider geometry has "
            "nearby components"
        )
    elif semantic_kind == "system_or_parent":
        status, confidence = "system_level_target", "high"
        reason = (
            "SIMBAD marks this target as a parent/system; provider components are "
            "contextual geometry"
        )
    elif semantic_kind in {"component", "subsystem"}:
        status, confidence = "semantic_component_label_unknown", "medium"
        reason = (
            "SIMBAD hierarchy says this is a component/subsystem, but no component "
            "label has been parsed yet"
        )
    else:
        status, confidence = "unclassified", "low"
        reason = f"unhandled semantic identity kind: {semantic_kind}"
    return {
        "status": status,
        "confidence": confidence,
        "reason": reason,
        "semantic_kind": semantic_kind,
        "semantic_main_id": semantic_identity["main_id"],
        "semantic_component": semantic_component,
        "nearest_component": nearest_label,
        "nearest_separation_arcsec": nearest_sep,
        "closest_companion_component": (
            None if closest_companion is None else closest_companion["component"]
        ),
        "closest_companion_separation_arcsec": (
            None if closest_companion is None
            else closest_companion.get("separation_from_nearest_arcsec")
        ),
        "matched_systems": len(systems),
        "component_counts": [len(system["components"]) for system in systems],
        "relationship_relevance_counts": relevance_counts,
        "evidence": _target_assignment_evidence(semantic_kind, nearest_component),
        "review_required": review_required,
    }


def target_component_assignment_summary(
    value: dict[str, object],
) -> dict[str, object]:
    return {
        "status": value["status"],
        "confidence": value["confidence"],
        "semantic_kind": value["semantic_kind"],
        "semantic_component": value["semantic_component"],
        "nearest_component": value["nearest_component"],
        "nearest_separation_arcsec": value["nearest_separation_arcsec"],
        "review_required": value["review_required"],
    }


def target_photometry_context(
    session: Session,
    target: Target,
    *,
    component_assignment: dict[str, object],
    closest_companion: dict[str, object] | None,
) -> dict[str, object]:
    measurements = current_measurements_for_target(session, target.id)
    measurements.sort(key=lambda value: (value.provider, value.band, value.id))
    nearest_pair_arcsec = (
        None if closest_companion is None
        else closest_companion.get("separation_from_nearest_arcsec")
    )
    assignment_status = str(component_assignment.get("status") or "")
    semantic_kind = str(component_assignment.get("semantic_kind") or "")
    if assignment_status == "system_level_target":
        target_level = "system"
    elif (
        assignment_status == "semantic_group_contains_nearest_component"
        and _component_label_is_group(
            str(component_assignment.get("semantic_component") or ""),
        )
    ):
        target_level = "system"
    elif semantic_kind in {"component", "subsystem"}:
        target_level = "component_or_subsystem"
    elif closest_companion is not None:
        target_level = "hierarchical_context"
    else:
        target_level = "single_or_unknown"

    rows = []
    likely_blended = []
    predicted_scope_counts: dict[str, int] = {}
    predicted_blend_counts: dict[str, int] = {}
    unresolved_components = []
    if (
        nearest_pair_arcsec is not None
        and nearest_pair_arcsec <= 1.0
        and target_level == "system"
    ):
        unresolved_components = sorted({
            str(component_assignment.get("nearest_component") or ""),
            str(component_assignment.get("closest_companion_component") or ""),
        } - {""})
    for measurement in measurements:
        resolution = measurement.resolution_major_arcsec
        if resolution is not None and nearest_pair_arcsec is not None:
            if resolution >= nearest_pair_arcsec:
                blend_prediction = "likely_blended_at_catalog_resolution"
                likely_blended.append(f"{measurement.provider}:{measurement.band}")
            else:
                blend_prediction = "likely_resolved_at_catalog_resolution"
        elif nearest_pair_arcsec is None:
            blend_prediction = "no_nearby_component_estimate"
        else:
            blend_prediction = "unknown_resolution"
        scope_prediction = photometry_scope_prediction(
            target_level=target_level,
            assignment_status=assignment_status,
            semantic_kind=semantic_kind,
            blend_prediction=blend_prediction,
            stored_ownership_scope=measurement.ownership_scope,
            stored_blend_state=measurement.blend_state,
        )
        scope = scope_prediction["predicted_ownership_scope"]
        blend = scope_prediction["predicted_blend_state"]
        predicted_scope_counts[scope] = predicted_scope_counts.get(scope, 0) + 1
        predicted_blend_counts[blend] = predicted_blend_counts.get(blend, 0) + 1
        rows.append({
            "provider": measurement.provider,
            "band": measurement.band,
            "resolution_major_arcsec": resolution,
            "resolution_minor_arcsec": measurement.resolution_minor_arcsec,
            "resolution_kind": measurement.resolution_kind,
            "resolution_reference": measurement.resolution_reference,
            "ownership_scope": measurement.ownership_scope,
            "stored_ownership_scope": measurement.ownership_scope,
            "stored_blend_state": measurement.blend_state,
            "stored_blend_reason": measurement.blend_reason,
            "resolution_blend_evidence": blend_prediction,
            **scope_prediction,
        })

    if not measurements:
        recommendation = "no current photometry measurements to assess"
    elif target_level == "system":
        recommendation = (
            "treat current photometry as system/subsystem-level unless a "
            "catalog-specific component association says otherwise"
        )
    elif likely_blended:
        recommendation = (
            "review low-resolution bands before interpreting component-level excess"
        )
    elif closest_companion is not None:
        recommendation = (
            "hierarchy is present but current band resolutions do not obviously "
            "force blending"
        )
    else:
        recommendation = "no hierarchy-driven photometry concern identified"

    return {
        "target_level": target_level,
        "nearest_pair_arcsec": nearest_pair_arcsec,
        "likely_unresolved_components": unresolved_components,
        "likely_blended_bands": likely_blended,
        "measurement_count": len(measurements),
        "predicted_scope_counts": dict(sorted(predicted_scope_counts.items())),
        "predicted_blend_counts": dict(sorted(predicted_blend_counts.items())),
        "bands": rows,
        "recommendation": recommendation,
        "review_required": bool(likely_blended and target_level != "system"),
    }


def refresh_photometry_band_summaries(photometry: dict[str, object]) -> None:
    bands = list(photometry.get("bands") or [])
    scope_counts: dict[str, int] = {}
    blend_counts: dict[str, int] = {}
    for band in bands:
        scope = str(band.get("predicted_ownership_scope") or "unknown")
        blend = str(band.get("predicted_blend_state") or "unknown")
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        blend_counts[blend] = blend_counts.get(blend, 0) + 1
    photometry["predicted_scope_counts"] = dict(sorted(scope_counts.items()))
    photometry["predicted_blend_counts"] = dict(sorted(blend_counts.items()))
    photometry["review_required"] = bool(
        photometry.get("likely_blended_bands")
        and photometry.get("target_level") != "system"
    )


def target_photometry_context_summary(
    value: dict[str, object],
) -> dict[str, object]:
    bands = value.get("bands") or []
    return {
        "target_level": value.get("target_level"),
        "nearest_pair_arcsec": value.get("nearest_pair_arcsec"),
        "measurement_count": value.get("measurement_count", 0),
        "likely_blended_bands": value.get("likely_blended_bands", []),
        "likely_unresolved_components": value.get(
            "likely_unresolved_components", [],
        ),
        "predicted_scope_counts": value.get("predicted_scope_counts", {}),
        "predicted_blend_counts": value.get("predicted_blend_counts", {}),
        "bands_with_resolution": sum(
            1 for band in bands
            if band.get("resolution_major_arcsec") is not None
        ),
        "recommendation": value.get("recommendation"),
        "review_required": value.get("review_required", False),
    }


def review_queue_row(
    context: dict[str, object],
    photometry: dict[str, object],
) -> dict[str, object]:
    systems = list(context["systems"])
    candidate_count = sum(len(system["candidates"]) for system in systems)
    accepted_count = sum(
        1 for system in systems for candidate in system["candidates"]
        if candidate["status"] == "accepted"
    )
    rejected_count = sum(
        1 for system in systems for candidate in system["candidates"]
        if candidate["status"] == "rejected"
    )
    diagnostic_count = sum(len(system["diagnostics"]) for system in systems)
    basis = str(context["hierarchy_decision_basis"])
    likely_blended = list(photometry["likely_blended_bands"])
    assignment_status = str(context["component_assignment"]["status"])
    review_required = bool(
        context["review_required"] or photometry["review_required"],
    )

    if likely_blended and basis == "candidate_review":
        priority = ReviewPriority.HIGHEST
        reason = "likely blended photometry depends on unaccepted hierarchy candidates"
    elif accepted_count == 0 and len(systems) > 1:
        priority = ReviewPriority.HIGH
        reason = "multiple candidate hierarchy systems need a decision"
    elif assignment_status == "semantic_geometry_conflict":
        priority = ReviewPriority.HIGH
        reason = "SIMBAD semantic component and provider geometry disagree"
    elif review_required or diagnostic_count:
        priority = ReviewPriority.MEDIUM
        reason = "hierarchy diagnostics or photometry context require review"
    elif candidate_count and accepted_count == 0:
        priority = ReviewPriority.LOW
        reason = "single clean hierarchy candidate has not been accepted"
    elif accepted_count:
        priority = ReviewPriority.LOW
        reason = "accepted hierarchy decision present"
    else:
        priority = ReviewPriority.NONE
        reason = "no hierarchy review item"

    sdbid = str(context["target"]["sdbid"])
    return {
        "sdbid": sdbid,
        "priority": priority.value,
        "reason": reason,
        "candidate_count": candidate_count,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "candidate_system_count": len(systems),
        "diagnostic_count": diagnostic_count,
        "basis": basis,
        "classification": context["classification"],
        "component_assignment_status": assignment_status,
        "likely_blended_bands": likely_blended,
        "nearest_pair_arcsec": photometry["nearest_pair_arcsec"],
        "review_required": review_required,
        "review_view_hint": f"sdb review-view {sdbid} --output {sdbid}-review.html",
    }


def _target_assignment_evidence(
    semantic_kind: str,
    nearest_component: dict[str, object] | None,
) -> list[str]:
    evidence = []
    if semantic_kind != "unknown":
        evidence.append("simbad_relationships")
    if nearest_component is not None:
        evidence.append("provider_geometry")
    return evidence


def _best_component_label_candidate(
    candidates: list[dict[str, object]],
) -> str | None:
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.get("source") == "main_id":
            return str(candidate["label"])
    return str(candidates[0]["label"])


def _component_labels_match(first: str, second: str) -> bool:
    return normalize_component_label(first) == normalize_component_label(second)


def _component_label_is_group(value: str) -> bool:
    label = normalize_component_label(value.strip())
    if not label:
        return False
    if "," in label:
        return True
    return len(label) > 1 and label.isalpha() and label.isupper()


def _component_label_contains(group: str, component: str) -> bool:
    group = normalize_component_label(group)
    component = normalize_component_label(component)
    if group == component:
        return True
    if "," in group:
        return any(
            _component_label_contains(part.strip(), component)
            for part in group.split(",")
        )
    if len(component) == 1 and len(group) > 1 and group.isalpha() and group.isupper():
        return component in group
    if component and group:
        return component[0] == group[0] and len(group) <= len(component)
    return False


def photometry_scope_prediction(
    *,
    target_level: str,
    assignment_status: str,
    semantic_kind: str,
    blend_prediction: str,
    stored_ownership_scope: str,
    stored_blend_state: str,
) -> dict[str, str]:
    if stored_ownership_scope != "component" or stored_blend_state != "clear":
        return {
            "predicted_ownership_scope": stored_ownership_scope,
            "predicted_blend_state": stored_blend_state,
            "predicted_blend_reason": "stored_catalog_state",
            "scope_reason": (
                "provider or existing catalog state already marks this measurement"
            ),
        }
    if assignment_status in {
        "review_required",
        "semantic_geometry_conflict",
        "ambiguous_disconnected_groups",
    }:
        return {
            "predicted_ownership_scope": "ambiguous",
            "predicted_blend_state": "ambiguous",
            "predicted_blend_reason": "hierarchy_ambiguous",
            "scope_reason": f"target assignment is {assignment_status}",
        }
    if target_level == "system" and blend_prediction == "likely_resolved_at_catalog_resolution":
        return {
            "predicted_ownership_scope": "component",
            "predicted_blend_state": "clear",
            "predicted_blend_reason": "resolved_at_catalog_resolution",
            "scope_reason": (
                "catalog resolution separates the nearest known components; the "
                "selected source is associated with the component at the target position"
            ),
        }
    if target_level == "system" and blend_prediction == "likely_blended_at_catalog_resolution":
        return {
            "predicted_ownership_scope": "system",
            "predicted_blend_state": "blended",
            "predicted_blend_reason": "unresolved_at_catalog_resolution",
            "scope_reason": (
                "target is a system and catalog resolution is larger than the nearest "
                "known component separation"
            ),
        }
    if target_level == "system":
        return {
            "predicted_ownership_scope": "system",
            "predicted_blend_state": "unknown",
            "predicted_blend_reason": "system_level_target",
            "scope_reason": (
                "SIMBAD/provider context identifies the target as a system or parent, "
                "and catalog resolution does not distinguish a component"
            ),
        }
    if blend_prediction == "likely_blended_at_catalog_resolution":
        return {
            "predicted_ownership_scope": "shared",
            "predicted_blend_state": "blended",
            "predicted_blend_reason": "unresolved_at_catalog_resolution",
            "scope_reason": (
                "catalog resolution is larger than the nearest known component separation"
            ),
        }
    if blend_prediction == "likely_resolved_at_catalog_resolution":
        return {
            "predicted_ownership_scope": "component",
            "predicted_blend_state": "clear",
            "predicted_blend_reason": "resolved_at_catalog_resolution",
            "scope_reason": (
                "catalog resolution is smaller than the nearest known component separation"
            ),
        }
    if semantic_kind in {"component", "subsystem"}:
        return {
            "predicted_ownership_scope": "component",
            "predicted_blend_state": "unknown",
            "predicted_blend_reason": "unknown_resolution",
            "scope_reason": (
                "target is semantically component-like, but band resolution is unavailable"
            ),
        }
    return {
        "predicted_ownership_scope": "component",
        "predicted_blend_state": "unknown",
        "predicted_blend_reason": blend_prediction,
        "scope_reason": (
            "no hierarchy/resolution evidence changes component-level interpretation"
        ),
    }
