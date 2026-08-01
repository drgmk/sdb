"""Derived hierarchy graph policy, projections, and diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .hierarchy_geometry import (
    hierarchy_separation_usable,
    offset_position,
    record_raw_payload,
    wds_record_has_unusable_separation,
)
from .hierarchy_wds import component_pair
from .models.hierarchy import (
    HierarchyMatchCandidate,
    HierarchyRecord,
    StructuralEdge,
    StructuralEdgeAction,
)


GRAPH_EDGE_STATUSES = ("derived", "stale", "rejected")


@dataclass(frozen=True)
class HierarchyGraphDeriveResult:
    provider: str
    source_id: int | None
    record_count: int
    edge_count: int
    skipped_count: int


@dataclass(frozen=True)
class HierarchyGraphEdgeRow:
    edge_id: int
    source_id: int
    record_id: int | None
    provider: str
    native_id: str
    source_component: str | None
    reference_label: str | None
    component_label: str | None
    relation_type: str
    structural_role: str
    status: str
    geometry_status: str
    start_ra_deg: float | None
    start_dec_deg: float | None
    end_ra_deg: float | None
    end_dec_deg: float | None
    separation_arcsec: float | None
    pa_deg: float | None
    relation_epoch: float | None
    note: str
    override_id: int | None = None
    override_actor: str | None = None
    override_reason: str | None = None


@dataclass(frozen=True)
class HierarchyGraphOverrideResult:
    override_id: int
    edge_id: int
    previous_status: str
    new_status: str
    previous_relation_type: str
    new_relation_type: str
    previous_structural_role: str
    new_structural_role: str
    actor: str
    reason: str


@dataclass(frozen=True)
class HierarchyGraphDiagnosticRow:
    provider: str
    source_id: int
    native_id: str
    issue: str
    severity: str
    edge_count: int
    structural_count: int
    non_structural_count: int
    matched_candidate_count: int
    detail: str


def build_wds_record_index(
    records: tuple[HierarchyRecord, ...],
) -> dict[tuple[int, str, str], HierarchyRecord]:
    index: dict[tuple[int, str, str], HierarchyRecord] = {}
    for record in records:
        if record.provider != "wds":
            continue
        component = (record.component or "").strip()
        if component:
            index.setdefault((record.source_id, record.native_id, component), record)
    return index


def edge_key(
    edge: StructuralEdge,
) -> tuple[int, str, str, str | None, str | None, str]:
    return (
        edge.source_id,
        edge.source,
        edge.native_id,
        edge.reference_label,
        edge.component_label,
        edge.relation_type,
    )


def copy_edge_values(existing: StructuralEdge, replacement: StructuralEdge) -> None:
    existing.record_id = replacement.record_id
    existing.source_component = replacement.source_component
    existing.structural_role = replacement.structural_role
    existing.status = replacement.status
    existing.geometry_status = replacement.geometry_status
    existing.start_ra_deg = replacement.start_ra_deg
    existing.start_dec_deg = replacement.start_dec_deg
    existing.end_ra_deg = replacement.end_ra_deg
    existing.end_dec_deg = replacement.end_dec_deg
    existing.separation_arcsec = replacement.separation_arcsec
    existing.pa_deg = replacement.pa_deg
    existing.relation_epoch = replacement.relation_epoch
    existing.note = replacement.note


def derive_wds_edge(
    record: HierarchyRecord,
    record_index: dict[tuple[int, str, str], HierarchyRecord],
) -> StructuralEdge | None:
    if record.provider != "wds":
        return None
    raw_payload = record_raw_payload(record)
    if wds_record_has_unusable_separation(record, raw_payload=raw_payload):
        return None
    source_component = (record.component or "").strip() or None
    reference, component = _wds_graph_component_pair(record, record_index)
    if not reference or not component:
        return None
    start = _wds_graph_start_position(record, record_index, reference)
    end: tuple[float, float] | None = None
    geometry_status = "missing"
    if (
        start is not None
        and record.separation_arcsec is not None
        and record.pa_deg is not None
        and hierarchy_separation_usable(record.provider, record.separation_arcsec)
    ):
        end = offset_position(
            start[0], start[1], record.separation_arcsec, record.pa_deg,
        )
        geometry_status = "usable"
    elif record.ra_deg is not None and record.dec_deg is not None:
        geometry_status = "base_only"
    relation_type = _wds_graph_relation_type(reference, component, source_component)
    return StructuralEdge(
        source=record.provider,
        source_id=record.source_id,
        record_id=record.id,
        native_id=record.native_id,
        source_component=source_component,
        reference_label=reference,
        component_label=component,
        relation_type=relation_type,
        structural_role=_default_structural_role(relation_type),
        status="derived",
        geometry_status=geometry_status,
        start_ra_deg=None if start is None else start[0],
        start_dec_deg=None if start is None else start[1],
        end_ra_deg=None if end is None else end[0],
        end_dec_deg=None if end is None else end[1],
        separation_arcsec=record.separation_arcsec,
        pa_deg=record.pa_deg,
        relation_epoch=record.measure_epoch,
        note=str(raw_payload.get("Notes") or raw_payload.get("notes") or ""),
    )


def demote_ambiguous_edges(edges: list[StructuralEdge]) -> None:
    parents_by_child: dict[tuple[int, str, str, str], set[str]] = {}
    for edge in edges:
        if edge.structural_role != "structural" or edge.relation_type != "group":
            continue
        child = conceptual_component(edge.component_label)
        parent = conceptual_component(edge.reference_label)
        if not child or not parent or child == parent:
            continue
        parents_by_child.setdefault(
            (edge.source_id, edge.source, edge.native_id, child), set(),
        ).add(parent)
    ambiguous_children = {
        key for key, parents in parents_by_child.items() if len(parents) > 1
    }
    for edge in edges:
        child = conceptual_component(edge.component_label)
        key = (edge.source_id, edge.source, edge.native_id, child)
        if key in ambiguous_children and edge.relation_type == "group":
            edge.structural_role = "non_structural"
            edge.note = _join_notes(
                edge.note, "ambiguous structural parent; demoted to non-structural",
            ) or ""


def latest_overrides(
    session: Session,
    edges: list[StructuralEdge],
) -> dict[int, StructuralEdgeAction]:
    if not edges:
        return {}
    sources = sorted({edge.source for edge in edges})
    native_ids = {edge.native_id for edge in edges if edge.native_id is not None}
    rows = tuple(
        row
        for row in session.scalars(
            select(StructuralEdgeAction)
            .where(StructuralEdgeAction.source.in_(sources))
            .order_by(StructuralEdgeAction.created_at, StructuralEdgeAction.id)
        )
        if row.native_id is None or row.native_id in native_ids
    )
    latest: dict[int, StructuralEdgeAction] = {}
    for edge in edges:
        for row in rows:
            if row.edge_id == edge.id or (
                row.source == edge.source
                and row.native_id == edge.native_id
                and row.reference_label == edge.reference_label
                and row.component_label == edge.component_label
            ):
                latest[edge.id] = row
    return latest


def edges_for_system(
    session: Session,
    *,
    provider: str,
    native_id: str,
    source_id: int,
) -> tuple[HierarchyGraphEdgeRow, ...]:
    edges = tuple(session.scalars(
        select(StructuralEdge)
        .where(StructuralEdge.source == provider.lower().strip())
        .where(StructuralEdge.native_id == native_id.strip())
        .where(StructuralEdge.source_id == source_id)
        .where(StructuralEdge.status.in_(GRAPH_EDGE_STATUSES))
        .order_by(
            StructuralEdge.source,
            StructuralEdge.native_id,
            StructuralEdge.reference_label,
            StructuralEdge.component_label,
            StructuralEdge.id,
        )
    ))
    overrides = latest_overrides(session, list(edges))
    return tuple(edge_row(edge, overrides.get(edge.id)) for edge in edges)


def diagnostics_for_system(
    session: Session,
    *,
    provider: str,
    native_id: str,
    source_id: int,
) -> tuple[HierarchyGraphDiagnosticRow, ...]:
    """Return diagnostics for one native system without opening another session."""
    rows = list(edges_for_system(
        session,
        provider=provider,
        native_id=native_id,
        source_id=source_id,
    ))
    matched_count = session.scalar(
        select(func.count(HierarchyMatchCandidate.id))
        .join(
            HierarchyRecord,
            HierarchyRecord.id == HierarchyMatchCandidate.record_id,
        )
        .where(
            HierarchyRecord.provider == provider,
            HierarchyRecord.source_id == source_id,
            HierarchyRecord.native_id == native_id,
        )
    ) or 0
    return build_diagnostics(
        rows,
        {(provider, source_id, native_id): int(matched_count)},
        limit=0,
    )


def edge_row(
    edge: StructuralEdge, override: StructuralEdgeAction | None,
) -> HierarchyGraphEdgeRow:
    relation_type = (
        override.new_relation_type
        if override is not None and override.new_relation_type
        else edge.relation_type
    )
    structural_role = (
        override.new_structural_role
        if override is not None and override.new_structural_role
        else edge.structural_role
    )
    status = (
        override.new_status
        if override is not None and override.new_status
        else edge.status
    )
    return HierarchyGraphEdgeRow(
        edge_id=edge.id,
        source_id=edge.source_id,
        record_id=edge.record_id,
        provider=edge.source,
        native_id=edge.native_id,
        source_component=edge.source_component,
        reference_label=edge.reference_label,
        component_label=edge.component_label,
        relation_type=relation_type,
        structural_role=structural_role,
        status=status,
        geometry_status=edge.geometry_status,
        start_ra_deg=edge.start_ra_deg,
        start_dec_deg=edge.start_dec_deg,
        end_ra_deg=edge.end_ra_deg,
        end_dec_deg=edge.end_dec_deg,
        separation_arcsec=edge.separation_arcsec,
        pa_deg=edge.pa_deg,
        relation_epoch=edge.relation_epoch,
        note=edge.note,
        override_id=None if override is None else override.id,
        override_actor=None if override is None else override.actor,
        override_reason=None if override is None else override.reason,
    )


def diagnostic_row(
    provider: str,
    source_id: int,
    native_id: str,
    issue: str,
    severity: str,
    rows: list[HierarchyGraphEdgeRow],
    structural_count: int,
    non_structural_count: int,
    matched_candidate_count: int,
    detail: str,
) -> HierarchyGraphDiagnosticRow:
    return HierarchyGraphDiagnosticRow(
        provider=provider,
        source_id=source_id,
        native_id=native_id,
        issue=issue,
        severity=severity,
        edge_count=len(rows),
        structural_count=structural_count,
        non_structural_count=non_structural_count,
        matched_candidate_count=matched_candidate_count,
        detail=detail,
    )


def build_diagnostics(
    rows: list[HierarchyGraphEdgeRow],
    candidate_counts: dict[tuple[str, int, str], int],
    *,
    limit: int = 100,
    severity: str | None = None,
    issue: str | None = None,
) -> tuple[HierarchyGraphDiagnosticRow, ...]:
    """Classify effective graph rows into operator-facing diagnostics."""
    grouped: dict[tuple[str, int, str], list[HierarchyGraphEdgeRow]] = {}
    for row in rows:
        grouped.setdefault((row.provider, row.source_id, row.native_id), []).append(row)

    diagnostics: list[HierarchyGraphDiagnosticRow] = []
    for key, group_rows in grouped.items():
        row_provider, row_source_id, native_id = key
        matched_count = int(candidate_counts.get(key, 0))
        active_structural = [
            row for row in group_rows
            if row.structural_role == "structural" and row.status != "rejected"
        ]
        non_structural = [row for row in group_rows if row not in active_structural]
        structural_count = len(active_structural)
        non_structural_count = len(non_structural)
        if matched_count and structural_count == 0:
            diagnostics.append(diagnostic_row(
                row_provider, row_source_id, native_id,
                "matched_without_structural_edges", "review", group_rows,
                structural_count, non_structural_count, matched_count,
                "matched hierarchy candidates exist, but no active structural graph edge remains",
            ))
        elif structural_count == 0 and non_structural_count > 0:
            diagnostics.append(diagnostic_row(
                row_provider, row_source_id, native_id,
                "only_non_structural_edges", "info", group_rows,
                structural_count, non_structural_count, matched_count,
                "graph has only non-structural/display edges",
            ))
        if active_structural:
            roots = structural_roots(active_structural)
            if len(roots) > 1:
                diagnostics.append(diagnostic_row(
                    row_provider, row_source_id, native_id,
                    "disconnected_structural_groups", "info", group_rows,
                    structural_count, non_structural_count, matched_count,
                    f"structural roots: {', '.join(sorted(roots))}",
                ))
            duplicate = duplicate_parents(active_structural)
            if duplicate:
                diagnostics.append(diagnostic_row(
                    row_provider, row_source_id, native_id,
                    "duplicate_structural_parent", "review", group_rows,
                    structural_count, non_structural_count, matched_count,
                    "; ".join(
                        f"{component} from {', '.join(sorted(parents))}"
                        for component, parents in sorted(duplicate.items())
                    ),
                ))
            geometry_problems = [
                row for row in active_structural
                if matched_count and row.geometry_status != "usable"
            ]
            if geometry_problems:
                diagnostics.append(diagnostic_row(
                    row_provider, row_source_id, native_id,
                    "structural_geometry_problem", "review", group_rows,
                    structural_count, non_structural_count, matched_count,
                    geometry_problem_detail(geometry_problems),
                ))

    if severity is not None:
        diagnostics = [row for row in diagnostics if row.severity == severity]
    if issue is not None:
        diagnostics = [row for row in diagnostics if row.issue == issue]
    diagnostics.sort(key=lambda row: (
        0 if row.severity == "review" else 1,
        row.provider,
        row.source_id,
        row.native_id,
        row.issue,
    ))
    return tuple(diagnostics if limit == 0 else diagnostics[:limit])


def structural_roots(rows: list[HierarchyGraphEdgeRow]) -> set[str]:
    references = {
        conceptual_component(row.reference_label)
        for row in rows
        if row.reference_label
    }
    components = {
        conceptual_component(row.component_label)
        for row in rows
        if row.component_label
    }
    return {label for label in references if label not in components}


def duplicate_parents(
    rows: list[HierarchyGraphEdgeRow],
) -> dict[str, set[str]]:
    parents_by_component: dict[str, set[str]] = {}
    for row in rows:
        if not row.component_label or not row.reference_label:
            continue
        component = conceptual_component(row.component_label)
        parent = conceptual_component(row.reference_label)
        if component == parent:
            continue
        parents_by_component.setdefault(component, set()).add(parent)
    return {
        component: parents
        for component, parents in parents_by_component.items()
        if len(parents) > 1
    }


def conceptual_component(label: str | None) -> str:
    text = (label or "").strip()
    if not text:
        return ""
    if "," in text:
        return ",".join(conceptual_component(part) for part in text.split(","))
    return text[0].upper()


def geometry_problem_detail(rows: list[HierarchyGraphEdgeRow]) -> str:
    details: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.source_component:
            label = f"{row.source_component} {row.reference_label}->{row.component_label}"
        else:
            label = f"blank Comp interpreted as {row.reference_label}->{row.component_label}"
        missing = []
        if row.separation_arcsec is None:
            missing.append("rho")
        if row.pa_deg is None:
            missing.append("PA")
        missing_text = f"; missing {', '.join(missing)}" if missing else ""
        detail = f"{label} geometry {row.geometry_status}{missing_text}"
        if detail not in seen:
            seen.add(detail)
            details.append(detail)
        if len(details) == 5:
            break
    return "; ".join(details)


def _wds_graph_component_pair(
    record: HierarchyRecord,
    record_index: dict[tuple[int, str, str], HierarchyRecord],
) -> tuple[str | None, str | None]:
    component = (record.component or "").strip()
    if not component:
        if (record.source_id, record.native_id, "AB") not in record_index:
            return "A", "B"
        return None, None
    return component_pair(component)


def _wds_graph_start_position(
    record: HierarchyRecord,
    record_index: dict[tuple[int, str, str], HierarchyRecord],
    reference: str,
) -> tuple[float, float] | None:
    if record.ra_deg is None or record.dec_deg is None:
        return None
    reference = reference.strip()
    if len(reference) <= 1 or "," in reference:
        return record.ra_deg, record.dec_deg
    group_record = record_index.get((record.source_id, record.native_id, reference))
    if (
        group_record is None
        or group_record.ra_deg is None
        or group_record.dec_deg is None
        or group_record.separation_arcsec is None
        or group_record.pa_deg is None
        or not hierarchy_separation_usable(
            group_record.provider, group_record.separation_arcsec,
        )
    ):
        return record.ra_deg, record.dec_deg
    endpoint = offset_position(
        group_record.ra_deg,
        group_record.dec_deg,
        group_record.separation_arcsec,
        group_record.pa_deg,
    )
    return _midpoint_position(
        group_record.ra_deg, group_record.dec_deg, endpoint[0], endpoint[1],
    )


def _midpoint_position(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float,
) -> tuple[float, float]:
    x1, y1, z1 = _unit_vector(ra1_deg, dec1_deg)
    x2, y2, z2 = _unit_vector(ra2_deg, dec2_deg)
    x = x1 + x2
    y = y1 + y2
    z = z1 + z2
    norm = math.sqrt(x * x + y * y + z * z)
    if norm == 0:
        return ra1_deg, dec1_deg
    ra = math.degrees(math.atan2(y / norm, x / norm)) % 360.0
    dec = math.degrees(math.asin(max(-1.0, min(1.0, z / norm))))
    return ra, dec


def _unit_vector(
    ra_deg: float, dec_deg: float,
) -> tuple[float, float, float]:
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    cos_dec = math.cos(dec)
    return cos_dec * math.cos(ra), cos_dec * math.sin(ra), math.sin(dec)


def _wds_graph_relation_type(
    reference: str, component: str, original_component: str | None,
) -> str:
    if _wds_component_group(reference) == _wds_component_group(component):
        return "internal"
    if _wds_structural_group_reference(reference, component, original_component):
        return "group"
    return "cross_link"


def _default_structural_role(relation_type: str) -> str:
    return "structural" if relation_type in {"group", "internal"} else "non_structural"


def _wds_component_group(label: str) -> str:
    text = label.strip()
    return text[0].upper() if text else ""


def _wds_structural_group_reference(
    reference: str, component: str, original_component: str | None,
) -> bool:
    reference = reference.strip()
    component = component.strip()
    original = (original_component or "").strip()
    if "," in original:
        return True
    return (
        len(reference) == 1
        and len(component) == 1
        and reference.isalpha()
        and component.isalpha()
        and reference.upper() != component.upper()
    )


def _join_notes(*values: str | None) -> str | None:
    notes = [value.strip() for value in values if value and value.strip()]
    return "; ".join(notes) if notes else None
