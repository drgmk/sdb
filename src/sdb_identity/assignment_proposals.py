from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import angular_separation_arcsec
from .adapters import catalog_source_id_matches_identifiers
from .catalog_measurements import current_measurement_encounters
from .dirty import find_target
from .models import (
    AstrometricSolution,
    ExternalIdentifier,
    MeasurementTargetAssociation,
    NormalizedMeasurement,
    RawCatalogRow,
    Target,
)
from .providers import Astrometry


_INACTIVE_STATES = {"suppressed", "superseded", "archived"}
_AMBIGUOUS_SCOPES = {"ambiguous", "neighbour_context", "reject"}


def measurement_assignment_proposals(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    system_context: dict[str, object] | None = None,
    target_context_loader: Callable[[str | int], dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Propose, but do not persist, system-level measurement assignments.

    Proposals deliberately use only evidence already visible during review:
    exact identifiers, catalog-row positions, per-band resolution, SIMBAD
    component semantics, and audited target lifecycle state.  An ambiguous
    proposal never changes the materialized assignment table.
    """
    if system_context is None or target_context_loader is None:
        from .hierarchy import HierarchyService

        hierarchy = HierarchyService(session_factory)
        if system_context is None:
            system_context = hierarchy.system_context(target_reference)
        if target_context_loader is None:
            target_context_loader = hierarchy.target_context

    with session_factory() as session:
        requested = find_target(session, target_reference)
        if requested is None:
            raise KeyError(f"target not found: {target_reference}")
        targets = _proposal_targets(session, requested, system_context)
        if not targets:
            targets = {requested.id: requested}
        target_ids = sorted(targets)
        encounters = current_measurement_encounters(session, target_ids)
        measurements_by_id = {
            row.measurement.id: row.measurement for row in encounters
        }
        measurements = sorted(measurements_by_id.values(), key=lambda value: (
            value.provider, value.source_id, value.band, value.id,
        ))
        encounter_targets: dict[int, set[int]] = {}
        raw_rows = {}
        for encounter in encounters:
            encounter_targets.setdefault(encounter.measurement.id, set()).add(
                encounter.target_id
            )
            raw_rows.setdefault(encounter.measurement.id, encounter.raw_row)
        identifiers = _target_identifiers(session, target_ids)
        target_astrometry = _target_astrometry(session, targets)
        current = _current_assignments(session, [value.id for value in measurements])

    semantic = system_context.get("simbad_semantic_by_target") or {}
    lifecycle = system_context.get("target_lifecycle_by_target") or {}
    target_contexts: dict[str, dict[str, object]] = {}
    result = []
    for measurement in measurements:
        origin = _proposal_origin(
            measurement,
            encounter_targets.get(measurement.id, {measurement.target_id}),
            targets=targets,
            identifiers=identifiers,
            semantic=semantic,
            lifecycle=lifecycle,
            target_astrometry=target_astrometry,
            raw=raw_rows.get(measurement.id),
        )
        origin_context = target_contexts.get(origin.sdbid)
        if origin_context is None:
            origin_context = target_context_loader(origin.sdbid)
            target_contexts[origin.sdbid] = origin_context
        prediction = _measurement_prediction(origin_context, measurement)
        raw = raw_rows.get(measurement.id)
        source_position = Astrometry(
            origin.ra2000_deg if raw is None else raw.ra_deg,
            origin.dec2000_deg if raw is None else raw.dec_deg,
            2000.0 if raw is None else raw.epoch,
        )
        candidates = _candidate_rows(
            targets,
            source_position=source_position,
            target_astrometry=target_astrometry,
            provider=measurement.provider,
            source_id=measurement.source_id,
            identifiers=identifiers,
            semantic=semantic,
            lifecycle=lifecycle,
        )
        prediction = _effective_prediction(
            measurement,
            origin=origin,
            prediction=prediction,
            candidates=candidates,
        )
        proposed, proposal_reason, confidence = _propose_assignments(
            measurement,
            origin=origin,
            prediction=prediction,
            candidates=candidates,
        )
        current_rows = current.get(measurement.id, [])
        proposed_keys = {(row["target_id"], row["role"]) for row in proposed}
        current_keys = {(row["target_id"], row["role"]) for row in current_rows}
        if not proposed:
            comparison = "review_required"
        elif (
            any(row["role"] == "composite_scope" for row in proposed)
            and not any(row["role"] == "contributor" for row in proposed)
        ):
            comparison = "partial_proposal"
        elif not current_rows:
            comparison = "unassigned"
        elif proposed_keys == current_keys:
            comparison = "agrees_with_current"
        else:
            comparison = "differs_from_current"
        result.append({
            "measurement_id": measurement.id,
            "origin_target_id": origin.id,
            "origin_sdbid": origin.sdbid,
            "encounter_target_ids": sorted(encounter_targets.get(measurement.id, ())),
            "encounter_sdbids": sorted(
                targets[target_id].sdbid
                for target_id in encounter_targets.get(measurement.id, ())
                if target_id in targets
            ),
            "provider": measurement.provider,
            "source_id": measurement.source_id,
            "band": measurement.band,
            "value": measurement.value,
            "error": measurement.error,
            "unit": measurement.unit,
            "resolution_major_arcsec": measurement.resolution_major_arcsec,
            "resolution_minor_arcsec": measurement.resolution_minor_arcsec,
            "excluded": measurement.excluded,
            "predicted_scope": prediction["predicted_association_scope"],
            "predicted_blend_status": prediction["predicted_scope_blend_status"],
            "scope_reason": prediction["scope_reason"],
            "proposal_confidence": confidence,
            "proposal_reason": proposal_reason,
            "comparison_to_current": comparison,
            "proposed_assignments": proposed,
            "current_assignments": current_rows,
            "candidate_targets": candidates,
        })
    return result


def measurement_assignment_matrix(
    system_context: dict[str, object],
    proposals: list[dict[str, object]],
) -> dict[str, object]:
    """Build a compact target-by-measurement review projection.

    This is presentation state only.  Cells retain current and proposed roles
    separately so rendering the matrix cannot be mistaken for accepting a
    proposal or changing the export projection.
    """
    requested = system_context.get("target") or {}
    requested_sdbid = str(requested.get("sdbid") or "")
    memberships = system_context.get("system_memberships_by_target") or {}
    requested_system_ids = {
        int(row["system_id"])
        for row in memberships.get(requested_sdbid, [])
    }
    included_sdbids = {requested_sdbid} if requested_sdbid else set()
    if requested_system_ids:
        included_sdbids.update(
            str(sdbid)
            for sdbid, rows in memberships.items()
            if any(int(row["system_id"]) in requested_system_ids for row in rows)
        )

    for proposal in proposals:
        if proposal.get("origin_sdbid"):
            included_sdbids.add(str(proposal["origin_sdbid"]))
        for key in ("current_assignments", "proposed_assignments"):
            included_sdbids.update(
                str(row["sdbid"])
                for row in proposal.get(key) or []
                if row.get("sdbid")
            )
        if not requested_system_ids:
            resolution = proposal.get("resolution_major_arcsec")
            tolerance = 3.0 if resolution is None else max(3.0, float(resolution))
            included_sdbids.update(
                str(row["sdbid"])
                for row in proposal.get("candidate_targets") or []
                if row.get("sdbid") and (
                    row.get("identifier_match")
                    or float(row.get("separation_arcsec") or 0.0) <= tolerance
                )
            )

    target_rows = {
        str(row["sdbid"]): row
        for row in system_context.get("nearby_sdb_targets") or []
    }
    semantics = system_context.get("simbad_semantic_by_target") or {}
    lifecycle = system_context.get("target_lifecycle_by_target") or {}
    target_ids: dict[str, int] = {}
    for row in target_rows.values():
        target_ids[str(row["sdbid"])] = int(row["target_id"])
    for proposal in proposals:
        target_ids[str(proposal["origin_sdbid"])] = int(proposal["origin_target_id"])
        for key in ("current_assignments", "proposed_assignments", "candidate_targets"):
            for row in proposal.get(key) or []:
                if row.get("sdbid") and row.get("target_id") is not None:
                    target_ids[str(row["sdbid"])] = int(row["target_id"])

    columns = []
    for sdbid in included_sdbids:
        semantic = semantics.get(sdbid) or {}
        lifecycle_row = lifecycle.get(sdbid) or {}
        role, role_basis = _target_role(lifecycle_row, semantic)
        member_rows = memberships.get(sdbid) or []
        relevant_memberships = [
            row for row in member_rows
            if not requested_system_ids or int(row["system_id"]) in requested_system_ids
        ]
        component_labels = list(dict.fromkeys(
            str(row["component_label"])
            for row in relevant_memberships
            if row.get("component_label")
        ))
        semantic_labels = semantic.get("component_label_candidates") or []
        semantic_label = next(
            (str(row["label"]) for row in semantic_labels if row.get("label")),
            None,
        )
        main_id = semantic.get("main_id")
        primary_system_name = next((
            str(row["system_name"])
            for row in relevant_memberships
            if row.get("is_primary") and row.get("system_name")
        ), None)
        label = (
            "/".join(component_labels)
            if component_labels else semantic_label or primary_system_name or main_id or sdbid
        )
        columns.append({
            "target_id": target_ids.get(sdbid),
            "sdbid": sdbid,
            "label": label,
            "main_id": main_id,
            "component_labels": component_labels,
            "role": role,
            "role_basis": role_basis,
            "state": str(lifecycle_row.get("state") or "active"),
            "is_requested_target": sdbid == requested_sdbid,
            "is_system_primary": any(bool(row.get("is_primary")) for row in relevant_memberships),
        })
    columns.sort(key=lambda row: (
        row["role"] != "physical",
        str(row["label"]),
        str(row["sdbid"]),
    ))

    detection_groups: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for proposal in proposals:
        key = (
            str(proposal["provider"]),
            str(proposal["source_id"]),
            str(proposal["band"]),
        )
        detection_groups.setdefault(key, []).append(proposal)

    rows = []
    for (provider, source_id, band), group in detection_groups.items():
        current_by_target = _assignments_by_target([
            row
            for proposal in group
            for row in proposal.get("current_assignments") or []
        ])
        proposed_by_target = _assignments_by_target([
            row
            for proposal in group
            for row in proposal.get("proposed_assignments") or []
        ])
        candidates_by_target: dict[str, list[dict[str, object]]] = {}
        for proposal in group:
            for candidate in proposal.get("candidate_targets") or []:
                if candidate.get("sdbid"):
                    candidates_by_target.setdefault(str(candidate["sdbid"]), []).append(candidate)
        proposal_signatures = [
            {
                (str(row["sdbid"]), str(row["role"]))
                for row in proposal.get("proposed_assignments") or []
                if row.get("sdbid")
            }
            for proposal in group
        ]
        duplicate_proposal_conflict = any(
            signature != proposal_signatures[0]
            for signature in proposal_signatures[1:]
        )
        cells = []
        for column in columns:
            sdbid = str(column["sdbid"])
            current_rows = current_by_target.get(sdbid, [])
            proposed_rows = proposed_by_target.get(sdbid, [])
            current_roles = sorted({str(row["role"]) for row in current_rows})
            proposed_roles = sorted({str(row["role"]) for row in proposed_rows})
            candidate_rows = candidates_by_target.get(sdbid, [])
            candidate = min(
                candidate_rows,
                key=lambda row: float(row.get("separation_arcsec") or 0.0),
                default=None,
            )
            target_proposal_signatures = [
                {
                    str(row["role"])
                    for row in proposal.get("proposed_assignments") or []
                    if row.get("sdbid") == sdbid
                }
                for proposal in group
            ]
            target_proposal_conflict = any(
                signature != target_proposal_signatures[0]
                for signature in target_proposal_signatures[1:]
            )
            if target_proposal_conflict:
                status = "differs"
            elif current_roles and current_roles == proposed_roles:
                status = "agrees"
            elif current_roles and proposed_roles:
                status = "differs"
            elif current_roles:
                status = "current_only"
            elif proposed_roles:
                status = "proposed"
            elif candidate is not None:
                status = "candidate"
            else:
                status = "empty"
            cells.append({
                "target_id": column["target_id"],
                "sdbid": sdbid,
                "status": status,
                "current_roles": current_roles,
                "proposed_roles": proposed_roles,
                "proposal_evidence": sorted({
                    str(row["evidence"])
                    for row in proposed_rows
                    if row.get("evidence")
                }),
                "duplicate_proposal_conflict": target_proposal_conflict,
                "identifier_match": any(bool(row.get("identifier_match")) for row in candidate_rows),
                "separation_arcsec": None if candidate is None else candidate.get("separation_arcsec"),
            })
        first = group[0]
        values = {
            (proposal.get("value"), proposal.get("error"), proposal.get("unit"))
            for proposal in group
        }
        comparisons = {str(proposal["comparison_to_current"]) for proposal in group}
        if duplicate_proposal_conflict:
            comparison = "duplicate_proposal_conflict"
        elif len(comparisons) == 1:
            comparison = next(iter(comparisons))
        else:
            comparison = "mixed_duplicate_state"
        scopes = sorted({str(proposal["predicted_scope"]) for proposal in group})
        blend_statuses = sorted({str(proposal["predicted_blend_status"]) for proposal in group})
        reasons = list(dict.fromkeys(str(proposal["proposal_reason"]) for proposal in group))
        rows.append({
            "measurement_id": first["measurement_id"],
            "measurement_ids": [proposal["measurement_id"] for proposal in group],
            "stored_measurement_count": len(group),
            "provider": provider,
            "source_id": source_id,
            "band": band,
            "value": first["value"],
            "error": first.get("error"),
            "unit": first["unit"],
            "values_consistent": len(values) == 1,
            "origin_sdbid": first["origin_sdbid"],
            "origin_sdbids": list(dict.fromkeys(str(proposal["origin_sdbid"]) for proposal in group)),
            "encounter_sdbids": sorted({
                str(sdbid)
                for proposal in group
                for sdbid in proposal.get("encounter_sdbids") or []
            }),
            "resolution_major_arcsec": first.get("resolution_major_arcsec"),
            "resolution_minor_arcsec": first.get("resolution_minor_arcsec"),
            "excluded": any(bool(proposal.get("excluded")) for proposal in group),
            "predicted_scope": scopes[0] if len(scopes) == 1 else "mixed",
            "predicted_scopes": scopes,
            "predicted_blend_status": blend_statuses[0] if len(blend_statuses) == 1 else "mixed",
            "predicted_blend_statuses": blend_statuses,
            "proposal_confidence": min(
                (str(proposal["proposal_confidence"]) for proposal in group),
                key=lambda value: {"low": 0, "medium": 1, "high": 2}.get(value, 0),
            ),
            "proposal_reason": " | ".join(reasons),
            "comparison_to_current": comparison,
            "duplicate_proposal_conflict": duplicate_proposal_conflict,
            "cells": cells,
        })

    comparison_counts: dict[str, int] = {}
    for row in rows:
        comparison = str(row["comparison_to_current"])
        comparison_counts[comparison] = comparison_counts.get(comparison, 0) + 1
    return {
        "columns": columns,
        "rows": rows,
        "summary": {
            "target_count": len(columns),
            "measurement_count": len(rows),
            "detection_count": len(rows),
            "stored_measurement_count": len(proposals),
            "encounter_count": sum(
                len(proposal.get("encounter_target_ids") or [])
                for proposal in proposals
            ),
            "duplicate_detection_count": sum(row["stored_measurement_count"] > 1 for row in rows),
            "comparison_counts": dict(sorted(comparison_counts.items())),
            "review_required": sum(
                row["comparison_to_current"] in {
                    "review_required", "differs_from_current", "partial_proposal",
                    "duplicate_proposal_conflict", "mixed_duplicate_state",
                }
                or not row["values_consistent"]
                for row in rows
            ),
        },
        "notes": [
            "read-only matrix; proposed cells are not persisted assignments",
            "current and proposed roles are retained separately",
            "rows sharing provider, source ID, and band are one detection; underlying measurement IDs remain listed",
        ],
    }


def _assignments_by_target(
    assignments: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for row in assignments:
        if row.get("sdbid"):
            result.setdefault(str(row["sdbid"]), []).append(row)
    return result


def _proposal_targets(
    session: Session,
    requested: Target,
    context: dict[str, object],
) -> dict[int, Target]:
    sdbids = {requested.sdbid}
    for row in context.get("component_positions") or []:
        if row.get("linked_sdbid"):
            sdbids.add(str(row["linked_sdbid"]))
    for row in context.get("measurement_assignments") or []:
        if row.get("origin_sdbid"):
            sdbids.add(str(row["origin_sdbid"]))
        for contributor in row.get("contributors") or []:
            if contributor.get("sdbid"):
                sdbids.add(str(contributor["sdbid"]))
    lifecycle = context.get("target_lifecycle_by_target") or {}
    sdbids.update(str(value) for value in lifecycle)
    return {
        target.id: target
        for target in session.scalars(select(Target).where(Target.sdbid.in_(sdbids)))
    }


def _proposal_origin(
    measurement: NormalizedMeasurement,
    encountered_target_ids: set[int],
    *,
    targets: dict[int, Target],
    identifiers: dict[int, tuple[tuple[str, str], ...]],
    semantic: dict[str, dict[str, object]],
    lifecycle: dict[str, dict[str, object]],
    target_astrometry: dict[int, Astrometry],
    raw: RawCatalogRow | None,
) -> Target:
    encountered = [
        targets[target_id]
        for target_id in encountered_target_ids
        if target_id in targets
    ]
    if not encountered:
        return targets.get(measurement.target_id) or next(iter(targets.values()))
    identifier_matches = []
    for target in encountered:
        sources = _matching_identifier_sources(
            measurement.provider,
            measurement.source_id,
            identifiers.get(target.id, ()),
        )
        if sources:
            identifier_matches.append((target, _identifier_authority(sources)))
    if identifier_matches:
        strongest = max(authority for _target, authority in identifier_matches)
        preferred = [
            target for target, authority in identifier_matches
            if authority == strongest
        ]
        if len(preferred) == 1:
            return preferred[0]
    roles = {
        target.id: _target_role(
            lifecycle.get(target.sdbid) or {},
            semantic.get(target.sdbid) or {},
        )[0]
        for target in encountered
    }
    if measurement.association_scope in {"system", "shared", "blended"}:
        composites = [target for target in encountered if roles[target.id] == "composite"]
        if len(composites) == 1:
            return composites[0]
    physical = [target for target in encountered if roles[target.id] == "physical"]
    if raw is not None and physical:
        source = Astrometry(raw.ra_deg, raw.dec_deg, raw.epoch)
        return min(physical, key=lambda target: angular_separation_arcsec(
            source, target_astrometry[target.id], epoch=raw.epoch,
        ))
    return min(encountered, key=lambda target: target.sdbid)


def _target_identifiers(
    session: Session, target_ids: list[int]
) -> dict[int, tuple[tuple[str, str], ...]]:
    values: dict[int, list[tuple[str, str]]] = {
        target_id: [] for target_id in target_ids
    }
    for row in session.scalars(select(ExternalIdentifier).where(
        ExternalIdentifier.target_id.in_(target_ids)
    )):
        values[row.target_id].append((row.value, row.source))
    return {target_id: tuple(rows) for target_id, rows in values.items()}


def _matching_identifier_sources(
    provider: str,
    source_id: str,
    identifiers: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Return the provenance of exact catalog identifiers on one target."""
    return tuple(sorted({
        source
        for value, source in identifiers
        if catalog_source_id_matches_identifiers(provider, source_id, (value,))
    }))


def _identifier_authority(sources: tuple[str, ...]) -> int:
    """Rank identifier provenance without hiding lower-authority matches.

    SIMBAD identifiers describe the identity of the named astronomical object.
    Provider-derived identifiers may instead have been attached by an earlier
    positional match, which must not outrank SIMBAD when both occur elsewhere
    in the same imported system.
    """
    return 2 if "simbad" in sources else 1


def _target_astrometry(
    session: Session,
    targets: dict[int, Target],
) -> dict[int, Astrometry]:
    solution_ids = {
        target.canonical_astrometry_id
        for target in targets.values()
        if target.canonical_astrometry_id is not None
    }
    solutions = {
        solution.id: solution
        for solution in session.scalars(select(AstrometricSolution).where(
            AstrometricSolution.id.in_(solution_ids)
        ))
    }
    result = {}
    for target in targets.values():
        solution = solutions.get(target.canonical_astrometry_id)
        result[target.id] = Astrometry(
            target.ra2000_deg,
            target.dec2000_deg,
            2000.0,
            pm_ra_cosdec_masyr=None if solution is None else solution.pm_ra_cosdec_masyr,
            pm_dec_masyr=None if solution is None else solution.pm_dec_masyr,
            source="sdb" if solution is None else solution.source,
        )
    return result


def _current_assignments(
    session: Session,
    measurement_ids: list[int],
) -> dict[int, list[dict[str, object]]]:
    result: dict[int, list[dict[str, object]]] = {}
    if not measurement_ids:
        return result
    rows = list(session.scalars(select(MeasurementTargetAssociation).where(
        MeasurementTargetAssociation.measurement_id.in_(measurement_ids)
    ).order_by(MeasurementTargetAssociation.id)))
    target_ids = {row.target_id for row in rows}
    targets = {
        target.id: target.sdbid
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
    }
    for row in rows:
        result.setdefault(row.measurement_id, []).append({
            "association_id": row.id,
            "target_id": row.target_id,
            "sdbid": targets.get(row.target_id),
            "role": row.role,
            "method": row.method,
            "weight": row.weight,
        })
    return result


def _measurement_prediction(
    context: dict[str, object],
    measurement: NormalizedMeasurement,
) -> dict[str, str]:
    bands = context.get("photometry_context", {}).get("bands", [])
    for row in bands:
        if row.get("provider") == measurement.provider and row.get("band") == measurement.band:
            return {
                "predicted_association_scope": str(row.get("predicted_association_scope") or "component"),
                "predicted_scope_blend_status": str(row.get("predicted_scope_blend_status") or "unknown"),
                "scope_reason": str(row.get("scope_reason") or ""),
            }
    return {
        "predicted_association_scope": measurement.association_scope,
        "predicted_scope_blend_status": measurement.blend_status,
        "scope_reason": "no hierarchy band prediction was available; retained stored catalog scope",
    }


def _candidate_rows(
    targets: dict[int, Target],
    *,
    source_position: Astrometry,
    target_astrometry: dict[int, Astrometry],
    provider: str,
    source_id: str,
    identifiers: dict[int, tuple[tuple[str, str], ...]],
    semantic: dict[str, dict[str, object]],
    lifecycle: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    rows = []
    for target in targets.values():
        lifecycle_row = lifecycle.get(target.sdbid) or {}
        semantic_row = semantic.get(target.sdbid) or {}
        role, role_basis = _target_role(lifecycle_row, semantic_row)
        state = str(lifecycle_row.get("state") or "active")
        identifier_sources = _matching_identifier_sources(
            provider, source_id, identifiers.get(target.id, ())
        )
        rows.append({
            "target_id": target.id,
            "sdbid": target.sdbid,
            "target_role": role,
            "target_role_basis": role_basis,
            "target_state": state,
            "eligible": state not in _INACTIVE_STATES,
            "identifier_match": bool(identifier_sources),
            "identifier_sources": list(identifier_sources),
            "identifier_authority": _identifier_authority(identifier_sources)
            if identifier_sources else 0,
            "separation_arcsec": angular_separation_arcsec(
                source_position,
                target_astrometry[target.id],
                epoch=source_position.epoch,
            ),
            "comparison_epoch": source_position.epoch,
        })
    strongest_authority = max(
        (int(row["identifier_authority"]) for row in rows), default=0
    )
    for row in rows:
        row["identifier_preferred"] = bool(
            row["identifier_match"]
            and int(row["identifier_authority"]) == strongest_authority
        )
    return sorted(rows, key=lambda row: (
        not bool(row["identifier_preferred"]),
        not bool(row["identifier_match"]),
        float(row["separation_arcsec"]),
        str(row["sdbid"]),
    ))


def _effective_prediction(
    measurement: NormalizedMeasurement,
    *,
    origin: Target,
    prediction: dict[str, str],
    candidates: list[dict[str, object]],
) -> dict[str, str]:
    """Add explicit-system lifecycle evidence absent from provider hierarchy."""
    if prediction["predicted_association_scope"] != "component":
        return prediction
    origin_row = next((row for row in candidates if row["target_id"] == origin.id), None)
    resolution = measurement.resolution_major_arcsec
    if origin_row is None or origin_row["target_role"] != "composite" or resolution is None:
        return prediction
    physical_in_beam = [
        row for row in candidates
        if row["eligible"]
        and row["target_role"] == "physical"
        and row["separation_arcsec"] <= resolution
    ]
    if len(physical_in_beam) < 2:
        return prediction
    return {
        "predicted_association_scope": "system",
        "predicted_scope_blend_status": "likely_blended_at_catalog_resolution",
        "scope_reason": (
            "the origin is an audited composite target and at least two physical "
            "system members lie within one stored full-width resolution"
        ),
    }


def _target_role(
    lifecycle: dict[str, object],
    semantic: dict[str, object],
) -> tuple[str, str]:
    explicit = str(lifecycle.get("role") or "unspecified")
    if explicit != "unspecified":
        return explicit, "target_lifecycle"
    kind = str(semantic.get("kind") or "unknown")
    labels = semantic.get("component_label_candidates") or []
    label = "" if not labels else str(labels[0].get("label") or "")
    if kind == "system_or_parent" or _group_component_label(label):
        return "composite", "simbad_semantics"
    return "physical", "default_or_component_semantics"


def _group_component_label(value: str) -> bool:
    value = value.strip()
    return bool("," in value or (len(value) > 1 and value.isalpha() and value.isupper()))


def _propose_assignments(
    measurement: NormalizedMeasurement,
    *,
    origin: Target,
    prediction: dict[str, str],
    candidates: list[dict[str, object]],
) -> tuple[list[dict[str, object]], str, str]:
    scope = prediction["predicted_association_scope"]
    eligible = [row for row in candidates if row["eligible"]]
    physical = [row for row in eligible if row["target_role"] == "physical"]
    composites = [row for row in eligible if row["target_role"] == "composite"]
    identifier_physical = [row for row in physical if row["identifier_preferred"]]
    identifier_composite = [row for row in composites if row["identifier_preferred"]]
    simbad_identifier_composite = [
        row for row in identifier_composite
        if "simbad" in (row.get("identifier_sources") or [])
    ]

    if scope in _AMBIGUOUS_SCOPES:
        return [], f"predicted scope {scope} requires review", "low"

    if scope == "component":
        if len(identifier_physical) == 1:
            return [_proposal(
                identifier_physical[0],
                "contributor",
                _identifier_evidence(identifier_physical[0]),
            )], (
                "one physical target has the preferred exact source identifier "
                f"({_identifier_source_label(identifier_physical[0])})"
            ), "high"
        if len(identifier_physical) > 1:
            return [], "the catalog source identifier belongs to multiple physical targets", "low"
        if len(simbad_identifier_composite) == 1:
            row = simbad_identifier_composite[0]
            return [_proposal(row, "composite_scope", "simbad_identifier")], (
                "the resolved catalog source has an exact SIMBAD identifier for "
                "the imported composite; its physical component contributor is not imported"
            ), "high"
        if len(simbad_identifier_composite) > 1:
            return [], (
                "the exact SIMBAD source identifier belongs to multiple imported composites"
            ), "low"
        tolerance = max(1.0, min(3.0, (measurement.resolution_major_arcsec or 2.0) / 2.0))
        positional = [row for row in physical if row["separation_arcsec"] <= tolerance]
        if len(positional) == 1:
            return [_proposal(positional[0], "contributor", "position")], (
                f"one physical target lies within the {tolerance:.2f} arcsec resolved-source tolerance"
            ), "medium"
        if len(positional) > 1:
            nearest = positional[0]
            if len(positional) == 1 or nearest["separation_arcsec"] + 0.5 < positional[1]["separation_arcsec"]:
                return [_proposal(nearest, "contributor", "position")], (
                    "nearest physical target is at least 0.5 arcsec closer than the alternative"
                ), "medium"
            return [], "multiple physical targets are positionally plausible for a resolved source", "low"
        origin_row = next((row for row in physical if row["target_id"] == origin.id), None)
        if origin_row is not None:
            return [_proposal(origin_row, "contributor", "origin_fallback")], (
                "no imported system member matches closely; retained the physical origin target"
            ), "low"
        return [], "resolved measurement has no imported physical target at its catalog position", "low"

    if scope in {"system", "blended", "shared"}:
        beam = measurement.resolution_major_arcsec
        composite_scopes = identifier_composite or [
            row for row in composites if row["target_id"] == origin.id
        ]
        if beam is None:
            assignments = [
                _proposal(row, "composite_scope", _identifier_evidence(row) if row["identifier_preferred"] else "origin_scope")
                for row in composite_scopes
            ]
            return assignments, (
                "the composite scope is identifiable, but missing resolution prevents "
                "selection of physical contributors"
            ), "low"
        contributors = [row for row in physical if row["separation_arcsec"] <= beam]
        assignments = [
            _proposal(
                row,
                "contributor",
                f"{_identifier_evidence(row)}+beam"
                if row["identifier_preferred"] else "beam",
            )
            for row in contributors
        ]
        assignments.extend(
            _proposal(
                row,
                "composite_scope",
                _identifier_evidence(row)
                if row["identifier_preferred"] else "origin_scope",
            )
            for row in composite_scopes
        )
        if not assignments:
            return [], f"no eligible system target lies within the {beam:.2f} arcsec review beam", "low"
        if not contributors:
            simbad_scopes = [
                row for row in composite_scopes
                if "simbad" in (row.get("identifier_sources") or [])
            ]
            if len(simbad_scopes) == 1:
                return assignments, (
                    "the composite scope has an exact SIMBAD source identifier; "
                    f"association is secure, but no imported physical contributor lies "
                    f"within the {beam:.2f} arcsec review beam"
                ), "high"
            return assignments, (
                f"the composite scope is identifiable, but no imported physical contributor "
                f"lies within the {beam:.2f} arcsec review beam"
            ), "low"
        confidence = "high" if identifier_physical or identifier_composite else "medium"
        reason = (
            f"physical targets within one stored full-width resolution "
            f"({beam:.2f} arcsec) contribute"
        )
        if composite_scopes:
            reason += "; the identified/origin composite is retained as measurement scope"
        preferred_identifiers = [*identifier_physical, *identifier_composite]
        if preferred_identifiers:
            provenance = sorted({
                _identifier_source_label(row) for row in preferred_identifiers
            })
            reason += "; preferred exact identifier provenance: " + ", ".join(provenance)
        return assignments, reason, confidence

    return [], f"unsupported predicted scope {scope}", "low"


def _proposal(row: dict[str, object], role: str, evidence: str) -> dict[str, object]:
    return {
        "target_id": row["target_id"],
        "sdbid": row["sdbid"],
        "role": role,
        "evidence": evidence,
        "identifier_match": row["identifier_match"],
        "identifier_preferred": row["identifier_preferred"],
        "identifier_sources": row["identifier_sources"],
        "separation_arcsec": row["separation_arcsec"],
    }


def _identifier_source_label(row: dict[str, object]) -> str:
    sources = [str(value) for value in row.get("identifier_sources") or []]
    return ", ".join(sources) if sources else "unknown provenance"


def _identifier_evidence(row: dict[str, object]) -> str:
    return "simbad_identifier" if "simbad" in (row.get("identifier_sources") or []) else "identifier"
