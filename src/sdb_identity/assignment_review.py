"""Read-only measurement-assignment review projections.

Proposal generation remains domain logic in :mod:`assignment_proposals`.
This module owns the presentation-oriented target-by-measurement matrix used by
the interactive review surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from sqlalchemy.orm import Session, sessionmaker

from .adapters import catalog_source_display_name
from .assignment_proposals import (
    effective_target_role,
    measurement_assignment_proposals,
)


class AssignmentMatrixColumn(TypedDict):
    target_id: int | None
    sdbid: str
    label: str
    main_id: object
    component_labels: list[str]
    role: str
    role_basis: str
    state: str
    is_requested_target: bool
    is_system_primary: bool


class AssignmentMatrixCell(TypedDict):
    target_id: int | None
    sdbid: str
    status: str
    current_roles: list[str]
    proposed_roles: list[str]
    proposal_evidence: list[str]
    duplicate_proposal_conflict: bool
    identifier_match: bool
    separation_arcsec: object


class AssignmentMatrixRow(TypedDict):
    measurement_id: object
    measurement_ids: list[object]
    stored_measurement_count: int
    provider: str
    source_id: str
    source_display_name: str
    band: str
    value: object
    error: object
    unit: object
    values_consistent: bool
    origin_sdbid: object
    origin_sdbids: list[str]
    encounter_sdbids: list[str]
    resolution_major_arcsec: object
    resolution_minor_arcsec: object
    excluded: bool
    predicted_scope: str
    predicted_scopes: list[str]
    predicted_blend_state: str
    predicted_blend_states: list[str]
    proposal_confidence: str
    proposal_reason: str
    comparison_to_current: str
    duplicate_proposal_conflict: bool
    cells: list[AssignmentMatrixCell]


class AssignmentMatrixSummary(TypedDict):
    target_count: int
    measurement_count: int
    stored_measurement_count: int
    encounter_count: int
    duplicate_measurement_group_count: int
    comparison_counts: dict[str, int]
    review_required: int


class MeasurementAssignmentMatrix(TypedDict):
    columns: list[AssignmentMatrixColumn]
    rows: list[AssignmentMatrixRow]
    summary: AssignmentMatrixSummary
    notes: list[str]


@dataclass(frozen=True)
class MeasurementAssignmentReview:
    proposals: list[dict[str, object]]
    matrix: MeasurementAssignmentMatrix


def build_measurement_assignment_review(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    system_context: dict[str, object] | None = None,
) -> MeasurementAssignmentReview:
    """Build proposal and matrix state for an interactive assignment review."""
    if system_context is None:
        from .hierarchy import HierarchyService

        system_context = HierarchyService(session_factory).system_context(
            target_reference
        )
    proposals = measurement_assignment_proposals(
        session_factory,
        target_reference,
        system_context=system_context,
    )
    return MeasurementAssignmentReview(
        proposals=proposals,
        matrix=measurement_assignment_matrix(system_context, proposals),
    )


def measurement_assignment_matrix(
    system_context: dict[str, object],
    proposals: list[dict[str, object]],
) -> MeasurementAssignmentMatrix:
    """Build the review-only target-by-measurement matrix."""
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
        target_ids[str(proposal["origin_sdbid"])] = int(
            proposal["origin_target_id"]
        )
        for key in (
            "current_assignments",
            "proposed_assignments",
            "candidate_targets",
        ):
            for row in proposal.get(key) or []:
                if row.get("sdbid") and row.get("target_id") is not None:
                    target_ids[str(row["sdbid"])] = int(row["target_id"])

    columns: list[AssignmentMatrixColumn] = []
    for sdbid in included_sdbids:
        semantic = semantics.get(sdbid) or {}
        lifecycle_row = lifecycle.get(sdbid) or {}
        role, role_basis = effective_target_role(lifecycle_row, semantic)
        member_rows = memberships.get(sdbid) or []
        relevant_memberships = [
            row for row in member_rows
            if not requested_system_ids
            or int(row["system_id"]) in requested_system_ids
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
            if component_labels
            else semantic_label or primary_system_name or main_id or sdbid
        )
        columns.append({
            "target_id": target_ids.get(sdbid),
            "sdbid": sdbid,
            "label": str(label),
            "main_id": main_id,
            "component_labels": component_labels,
            "role": role,
            "role_basis": role_basis,
            "state": str(lifecycle_row.get("state") or "active"),
            "is_requested_target": sdbid == requested_sdbid,
            "is_system_primary": any(
                bool(row.get("is_primary")) for row in relevant_memberships
            ),
        })
    columns.sort(key=lambda row: (
        row["role"] != "physical",
        row["label"],
        row["sdbid"],
    ))

    measurement_groups: dict[
        tuple[str, str, str], list[dict[str, object]]
    ] = {}
    for proposal in proposals:
        key = (
            str(proposal["provider"]),
            str(proposal["source_id"]),
            str(proposal["band"]),
        )
        measurement_groups.setdefault(key, []).append(proposal)

    rows: list[AssignmentMatrixRow] = []
    for (provider, source_id, band), group in measurement_groups.items():
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
                    candidates_by_target.setdefault(
                        str(candidate["sdbid"]), []
                    ).append(candidate)
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
        cells: list[AssignmentMatrixCell] = []
        for column in columns:
            sdbid = column["sdbid"]
            current_rows = current_by_target.get(sdbid, [])
            proposed_rows = proposed_by_target.get(sdbid, [])
            current_roles = sorted({
                str(row["role"]) for row in current_rows
            })
            proposed_roles = sorted({
                str(row["role"]) for row in proposed_rows
            })
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
                "identifier_match": any(
                    bool(row.get("identifier_match"))
                    for row in candidate_rows
                ),
                "separation_arcsec": (
                    None if candidate is None
                    else candidate.get("separation_arcsec")
                ),
            })
        first = group[0]
        values = {
            (proposal.get("value"), proposal.get("error"), proposal.get("unit"))
            for proposal in group
        }
        comparisons = {
            str(proposal["comparison_to_current"]) for proposal in group
        }
        if duplicate_proposal_conflict:
            comparison = "duplicate_proposal_conflict"
        elif len(comparisons) == 1:
            comparison = next(iter(comparisons))
        else:
            comparison = "mixed_duplicate_state"
        scopes = sorted({
            str(proposal["predicted_scope"]) for proposal in group
        })
        blend_states = sorted({
            str(proposal["predicted_blend_state"]) for proposal in group
        })
        reasons = list(dict.fromkeys(
            str(proposal["proposal_reason"]) for proposal in group
        ))
        rows.append({
            "measurement_id": first["measurement_id"],
            "measurement_ids": [
                proposal["measurement_id"] for proposal in group
            ],
            "stored_measurement_count": len(group),
            "provider": provider,
            "source_id": source_id,
            "source_display_name": catalog_source_display_name(
                provider, source_id
            ),
            "band": band,
            "value": first["value"],
            "error": first.get("error"),
            "unit": first["unit"],
            "values_consistent": len(values) == 1,
            "origin_sdbid": first["origin_sdbid"],
            "origin_sdbids": list(dict.fromkeys(
                str(proposal["origin_sdbid"]) for proposal in group
            )),
            "encounter_sdbids": sorted({
                str(sdbid)
                for proposal in group
                for sdbid in proposal.get("encounter_sdbids") or []
            }),
            "resolution_major_arcsec": first.get(
                "resolution_major_arcsec"
            ),
            "resolution_minor_arcsec": first.get(
                "resolution_minor_arcsec"
            ),
            "excluded": any(
                bool(proposal.get("excluded")) for proposal in group
            ),
            "predicted_scope": (
                scopes[0] if len(scopes) == 1 else "mixed"
            ),
            "predicted_scopes": scopes,
            "predicted_blend_state": (
                blend_states[0] if len(blend_states) == 1 else "mixed"
            ),
            "predicted_blend_states": blend_states,
            "proposal_confidence": min(
                (
                    str(proposal["proposal_confidence"])
                    for proposal in group
                ),
                key=lambda value: {
                    "low": 0, "medium": 1, "high": 2
                }.get(value, 0),
            ),
            "proposal_reason": " | ".join(reasons),
            "comparison_to_current": comparison,
            "duplicate_proposal_conflict": duplicate_proposal_conflict,
            "cells": cells,
        })

    comparison_counts: dict[str, int] = {}
    for row in rows:
        comparison = row["comparison_to_current"]
        comparison_counts[comparison] = (
            comparison_counts.get(comparison, 0) + 1
        )
    return {
        "columns": columns,
        "rows": rows,
        "summary": {
            "target_count": len(columns),
            "measurement_count": len(rows),
            "stored_measurement_count": len(proposals),
            "encounter_count": sum(
                len(proposal.get("encounter_target_ids") or [])
                for proposal in proposals
            ),
            "duplicate_measurement_group_count": sum(
                row["stored_measurement_count"] > 1 for row in rows
            ),
            "comparison_counts": dict(sorted(comparison_counts.items())),
            "review_required": sum(
                row["comparison_to_current"] in {
                    "review_required",
                    "differs_from_current",
                    "partial_proposal",
                    "duplicate_proposal_conflict",
                    "mixed_duplicate_state",
                }
                or not row["values_consistent"]
                for row in rows
            ),
        },
        "notes": [
            "read-only matrix; proposed cells are not persisted assignments",
            "current and proposed roles are retained separately",
            (
                "rows sharing provider, source ID, and band are one measurement "
                "group; underlying measurement IDs remain listed"
            ),
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
