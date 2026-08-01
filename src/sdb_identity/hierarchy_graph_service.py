from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from .decisions import DecisionContext
from .hierarchy_graph import (
    GRAPH_EDGE_STATUSES,
    HierarchyGraphDeriveResult,
    HierarchyGraphDiagnosticRow,
    HierarchyGraphEdgeRow,
    HierarchyGraphOverrideResult,
    build_diagnostics,
    build_wds_record_index,
    copy_edge_values,
    demote_ambiguous_edges,
    derive_wds_edge,
    edge_key,
    edge_row,
    latest_overrides,
)
from .models.hierarchy import (
    HierarchyMatchCandidate,
    HierarchyRecord,
    StructuralEdge,
    StructuralEdgeAction,
)
from .models.identity import Target
from .targets import resolve_target


class HierarchyGraphService:
    """Persist, query, diagnose, and override the derived hierarchy graph."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def derive_graph(
        self,
        provider: str,
        *,
        source_id: int | None = None,
    ) -> HierarchyGraphDeriveResult:
        provider = provider.lower().strip()
        if provider != "wds":
            raise ValueError(f"unsupported hierarchy graph provider: {provider}")
        with self.session_factory.begin() as session:
            query = select(HierarchyRecord).where(
                HierarchyRecord.provider == provider
            )
            if source_id is not None:
                query = query.where(HierarchyRecord.source_id == source_id)
            records = tuple(
                session.scalars(
                    query.order_by(
                        HierarchyRecord.source_id,
                        HierarchyRecord.native_id,
                        HierarchyRecord.id,
                    )
                )
            )
            existing_edges = {
                edge_key(edge): edge
                for edge in session.scalars(
                    select(StructuralEdge).where(
                        StructuralEdge.source == provider,
                        StructuralEdge.status.in_(GRAPH_EDGE_STATUSES),
                        *(
                            []
                            if source_id is None
                            else [StructuralEdge.source_id == source_id]
                        ),
                    )
                )
            }
            record_index = build_wds_record_index(records)
            skipped_count = 0
            derived_edges: list[StructuralEdge] = []
            for record in records:
                edge = derive_wds_edge(record, record_index)
                if edge is None:
                    skipped_count += 1
                else:
                    derived_edges.append(edge)
            demote_ambiguous_edges(derived_edges)

            refreshed_edge_ids: set[int] = set()
            for edge in derived_edges:
                existing = existing_edges.get(edge_key(edge))
                if existing is None:
                    session.add(edge)
                    session.flush()
                    refreshed_edge_ids.add(edge.id)
                else:
                    copy_edge_values(existing, edge)
                    refreshed_edge_ids.add(existing.id)

            stale_ids = sorted(
                {edge.id for edge in existing_edges.values()} - refreshed_edge_ids
            )
            if stale_ids:
                referenced_stale_ids = set(
                    session.scalars(
                        select(StructuralEdgeAction.edge_id).where(
                            StructuralEdgeAction.edge_id.in_(stale_ids)
                        )
                    )
                )
                for edge in existing_edges.values():
                    if edge.id in referenced_stale_ids:
                        edge.status = "stale"
                        edge.structural_role = "non_structural"
                        edge.note = (
                            _join_notes(
                                edge.note,
                                "stale graph edge retained because overrides reference it",
                            )
                            or ""
                        )
                deletable_stale_ids = sorted(
                    set(stale_ids) - referenced_stale_ids
                )
            else:
                deletable_stale_ids = []
            if deletable_stale_ids:
                session.execute(
                    delete(StructuralEdge).where(
                        StructuralEdge.id.in_(deletable_stale_ids)
                    )
                )
            return HierarchyGraphDeriveResult(
                provider=provider,
                source_id=source_id,
                record_count=len(records),
                edge_count=len(derived_edges),
                skipped_count=skipped_count,
            )

    def graph_edges(
        self,
        *,
        provider: str | None = None,
        native_id: str | None = None,
        target: str | int | None = None,
        source_id: int | None = None,
    ) -> tuple[HierarchyGraphEdgeRow, ...]:
        with self.session_factory() as session:
            query = select(StructuralEdge).where(
                StructuralEdge.status.in_(GRAPH_EDGE_STATUSES)
            )
            if provider is not None:
                query = query.where(StructuralEdge.source == provider.lower().strip())
            if native_id is not None:
                query = query.where(StructuralEdge.native_id == native_id.strip())
            if source_id is not None:
                query = query.where(StructuralEdge.source_id == source_id)
            if target is not None:
                target_row = _find_required_target(session, target)
                record_ids = tuple(
                    session.scalars(
                        select(HierarchyMatchCandidate.record_id)
                        .where(HierarchyMatchCandidate.target_id == target_row.id)
                        .where(
                            HierarchyMatchCandidate.status.in_(
                                ["candidate", "accepted"]
                            )
                        )
                    )
                )
                if not record_ids:
                    return ()
                query = query.where(StructuralEdge.record_id.in_(record_ids))
            edges = tuple(
                session.scalars(
                    query.order_by(
                        StructuralEdge.source,
                        StructuralEdge.native_id,
                        StructuralEdge.reference_label,
                        StructuralEdge.component_label,
                        StructuralEdge.id,
                    )
                )
            )
            overrides = latest_overrides(session, list(edges))
            return tuple(edge_row(edge, overrides.get(edge.id)) for edge in edges)

    def graph_diagnostics(
        self,
        *,
        provider: str | None = None,
        source_id: int | None = None,
        native_id: str | None = None,
        limit: int = 100,
        severity: str | None = None,
        issue: str | None = None,
    ) -> tuple[HierarchyGraphDiagnosticRow, ...]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        severity_value = None if severity is None else severity.lower().strip()
        issue_value = None if issue is None else issue.strip()
        with self.session_factory() as session:
            query = select(StructuralEdge).where(
                StructuralEdge.status.in_(GRAPH_EDGE_STATUSES)
            )
            provider_value = None if provider is None else provider.lower().strip()
            if provider_value is not None:
                query = query.where(StructuralEdge.source == provider_value)
            if source_id is not None:
                query = query.where(StructuralEdge.source_id == source_id)
            if native_id is not None:
                query = query.where(StructuralEdge.native_id == native_id.strip())
            edges = tuple(
                session.scalars(
                    query.order_by(
                        StructuralEdge.source,
                        StructuralEdge.source_id,
                        StructuralEdge.native_id,
                        StructuralEdge.id,
                    )
                )
            )
            overrides = latest_overrides(session, list(edges))
            rows = [edge_row(edge, overrides.get(edge.id)) for edge in edges]

            candidate_query = (
                select(
                    HierarchyRecord.provider,
                    HierarchyRecord.source_id,
                    HierarchyRecord.native_id,
                    func.count(HierarchyMatchCandidate.id),
                )
                .join(
                    HierarchyMatchCandidate,
                    HierarchyMatchCandidate.record_id == HierarchyRecord.id,
                )
                .group_by(
                    HierarchyRecord.provider,
                    HierarchyRecord.source_id,
                    HierarchyRecord.native_id,
                )
            )
            if provider_value is not None:
                candidate_query = candidate_query.where(
                    HierarchyRecord.provider == provider_value
                )
            if source_id is not None:
                candidate_query = candidate_query.where(
                    HierarchyRecord.source_id == source_id
                )
            if native_id is not None:
                candidate_query = candidate_query.where(
                    HierarchyRecord.native_id == native_id.strip()
                )
            candidate_counts = {
                (provider, current_source_id, current_native_id): count
                for provider, current_source_id, current_native_id, count
                in session.execute(candidate_query)
            }
        return build_diagnostics(
            rows,
            candidate_counts,
            limit=limit,
            severity=severity_value,
            issue=issue_value,
        )

    def override_graph_edge(
        self,
        *,
        provider: str,
        native_id: str,
        reference_label: str,
        component_label: str,
        actor: str | None,
        reason: str | None = None,
        source_id: int | None = None,
        status: str | None = None,
        relation_type: str | None = None,
        structural_role: str | None = None,
    ) -> HierarchyGraphOverrideResult:
        clean_provider = provider.lower().strip()
        clean_native = native_id.strip()
        clean_reference = reference_label.strip()
        clean_component = component_label.strip()
        clean_status = status.strip() if status is not None else None
        clean_relation_type = relation_type.strip() if relation_type is not None else None
        clean_structural_role = (
            structural_role.strip() if structural_role is not None else None
        )
        if clean_structural_role is not None and clean_structural_role not in {
            "structural",
            "non_structural",
        }:
            raise ValueError("structural role must be structural or non_structural")
        if (
            clean_status is None
            and clean_relation_type is None
            and clean_structural_role is None
        ):
            raise ValueError(
                "status, relation type, or structural role override is required"
            )
        with self.session_factory.begin() as session:
            query = select(StructuralEdge).where(
                StructuralEdge.source == clean_provider,
                StructuralEdge.native_id == clean_native,
                StructuralEdge.reference_label == clean_reference,
                StructuralEdge.component_label == clean_component,
                StructuralEdge.status.in_(GRAPH_EDGE_STATUSES),
            )
            if source_id is not None:
                query = query.where(StructuralEdge.source_id == source_id)
            matches = tuple(session.scalars(query.order_by(StructuralEdge.id)))
            if not matches:
                raise KeyError(
                    "hierarchy graph edge not found: "
                    f"{clean_provider} {clean_native} "
                    f"{clean_reference}->{clean_component}"
                )
            if len(matches) > 1 and source_id is None:
                raise ValueError("multiple graph edges matched; supply --source-id")
            edge = matches[0]
            requested = ", ".join(
                f"{name}={value}"
                for name, value in (
                    ("status", clean_status),
                    ("type", clean_relation_type),
                    ("role", clean_structural_role),
                )
                if value is not None
            )
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Overrode {edge.source} structural edge {edge.native_id} "
                    f"{edge.reference_label}->{edge.component_label}: {requested}"
                ),
            )
            latest = latest_overrides(session, [edge]).get(edge.id)
            previous_status = (
                latest.new_status
                if latest is not None and latest.new_status is not None
                else edge.status
            )
            previous_relation_type = (
                latest.new_relation_type
                if latest is not None and latest.new_relation_type is not None
                else edge.relation_type
            )
            previous_structural_role = (
                latest.new_structural_role
                if latest is not None and latest.new_structural_role is not None
                else edge.structural_role
            )
            override = StructuralEdgeAction(
                edge_id=edge.id,
                source=edge.source,
                native_id=edge.native_id,
                reference_label=edge.reference_label,
                component_label=edge.component_label,
                action="override_edge",
                previous_status=previous_status,
                new_status=clean_status,
                previous_relation_type=previous_relation_type,
                new_relation_type=clean_relation_type,
                previous_structural_role=previous_structural_role,
                new_structural_role=clean_structural_role,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(override)
            session.flush()
            return HierarchyGraphOverrideResult(
                override_id=override.id,
                edge_id=edge.id,
                previous_status=previous_status,
                new_status=clean_status or previous_status,
                previous_relation_type=previous_relation_type,
                new_relation_type=clean_relation_type or previous_relation_type,
                previous_structural_role=previous_structural_role,
                new_structural_role=clean_structural_role or previous_structural_role,
                actor=decision.actor,
                reason=decision.reason,
            )


def _find_required_target(
    session: Session,
    reference: str | int | None,
) -> Target:
    if reference is None:
        raise ValueError("target reference is required")
    target = resolve_target(session, reference)
    if target is None:
        raise KeyError(f"target not found: {reference}")
    return target


def _join_notes(*values: str | None) -> str | None:
    notes = [value.strip() for value in values if value and value.strip()]
    return "; ".join(notes) if notes else None
