from __future__ import annotations

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session, sessionmaker

from .graph import GRAPH_EDGE_STATUSES
from ..models.hierarchy import (
    HierarchyMatchCandidate,
    HierarchyRecord,
    HierarchySource,
    StructuralEdge,
)


class HierarchyReportingService:
    """Aggregate hierarchy source, matching, and graph health."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def summary(
        self,
        provider: str | None = None,
        *,
        source_id: int | None = None,
    ) -> dict[str, object]:
        provider_value = None if provider is None else provider.lower().strip()
        with self.session_factory() as session:
            source_query = (
                select(HierarchySource, func.count(HierarchyRecord.id))
                .join(
                    HierarchyRecord,
                    HierarchyRecord.source_id == HierarchySource.id,
                    isouter=True,
                )
                .group_by(HierarchySource.id)
                .order_by(HierarchySource.provider, HierarchySource.id)
            )
            candidate_status_query = (
                select(
                    HierarchyMatchCandidate.provider,
                    HierarchyMatchCandidate.status,
                    func.count(HierarchyMatchCandidate.id),
                )
                .join(
                    HierarchyRecord,
                    HierarchyRecord.id == HierarchyMatchCandidate.record_id,
                )
                .group_by(
                    HierarchyMatchCandidate.provider,
                    HierarchyMatchCandidate.status,
                )
                .order_by(
                    HierarchyMatchCandidate.provider,
                    HierarchyMatchCandidate.status,
                )
            )
            candidate_method_query = (
                select(
                    HierarchyMatchCandidate.provider,
                    HierarchyMatchCandidate.match_method,
                    func.count(HierarchyMatchCandidate.id),
                )
                .join(
                    HierarchyRecord,
                    HierarchyRecord.id == HierarchyMatchCandidate.record_id,
                )
                .group_by(
                    HierarchyMatchCandidate.provider,
                    HierarchyMatchCandidate.match_method,
                )
                .order_by(
                    HierarchyMatchCandidate.provider,
                    HierarchyMatchCandidate.match_method,
                )
            )
            record_count_query = (
                select(HierarchyRecord.provider, func.count(HierarchyRecord.id))
                .group_by(HierarchyRecord.provider)
                .order_by(HierarchyRecord.provider)
            )
            graph_relation_query = _graph_count_query(StructuralEdge.relation_type)
            graph_status_query = _graph_count_query(StructuralEdge.status)
            graph_geometry_query = _graph_count_query(StructuralEdge.geometry_status)
            graph_role_query = _graph_count_query(StructuralEdge.structural_role)
            matched_subquery = _matched_record_subquery()
            matched_query = _matched_record_query(matched_subquery)

            if provider_value is not None:
                source_query = source_query.where(
                    HierarchySource.provider == provider_value
                )
                candidate_status_query = candidate_status_query.where(
                    HierarchyMatchCandidate.provider == provider_value
                )
                candidate_method_query = candidate_method_query.where(
                    HierarchyMatchCandidate.provider == provider_value
                )
                record_count_query = record_count_query.where(
                    HierarchyRecord.provider == provider_value
                )
                graph_relation_query = graph_relation_query.where(
                    StructuralEdge.source == provider_value
                )
                graph_status_query = graph_status_query.where(
                    StructuralEdge.source == provider_value
                )
                graph_geometry_query = graph_geometry_query.where(
                    StructuralEdge.source == provider_value
                )
                graph_role_query = graph_role_query.where(
                    StructuralEdge.source == provider_value
                )
                matched_query = matched_query.where(
                    matched_subquery.c.provider == provider_value
                )
            if source_id is not None:
                source_query = source_query.where(HierarchySource.id == source_id)
                candidate_status_query = candidate_status_query.where(
                    HierarchyRecord.source_id == source_id
                )
                candidate_method_query = candidate_method_query.where(
                    HierarchyRecord.source_id == source_id
                )
                record_count_query = record_count_query.where(
                    HierarchyRecord.source_id == source_id
                )
                graph_relation_query = graph_relation_query.where(
                    StructuralEdge.source_id == source_id
                )
                graph_status_query = graph_status_query.where(
                    StructuralEdge.source_id == source_id
                )
                graph_geometry_query = graph_geometry_query.where(
                    StructuralEdge.source_id == source_id
                )
                graph_role_query = graph_role_query.where(
                    StructuralEdge.source_id == source_id
                )
                matched_subquery = _matched_record_subquery(source_id=source_id)
                matched_query = _matched_record_query(matched_subquery)
                if provider_value is not None:
                    matched_query = matched_query.where(
                        matched_subquery.c.provider == provider_value
                    )

            record_counts = {
                current_provider: count
                for current_provider, count in session.execute(record_count_query)
            }
            matched_counts = {
                current_provider: {
                    "matched_records": matched_records,
                    "ambiguous_records": ambiguous_records or 0,
                }
                for current_provider, matched_records, ambiguous_records
                in session.execute(matched_query)
            }
            return {
                "provider": provider_value,
                "source_id": source_id,
                "sources": [
                    {
                        "source_id": source.id,
                        "provider": source.provider,
                        "release": source.release,
                        "record_count": count,
                        "source_file": source.source_file,
                        "fetched_at": None
                        if source.fetched_at is None
                        else source.fetched_at.isoformat(),
                        "imported_at": source.imported_at.isoformat(),
                    }
                    for source, count in session.execute(source_query)
                ],
                "record_counts": [
                    {
                        "provider": current_provider,
                        "record_count": count,
                        "matched_records": matched_counts.get(
                            current_provider, {}
                        ).get("matched_records", 0),
                        "unmatched_records": count
                        - matched_counts.get(current_provider, {}).get(
                            "matched_records", 0
                        ),
                        "ambiguous_records": matched_counts.get(
                            current_provider, {}
                        ).get("ambiguous_records", 0),
                    }
                    for current_provider, count in record_counts.items()
                ],
                "candidate_status_counts": [
                    {"provider": p, "status": status, "count": count}
                    for p, status, count in session.execute(candidate_status_query)
                ],
                "candidate_method_counts": [
                    {"provider": p, "match_method": method, "count": count}
                    for p, method, count in session.execute(candidate_method_query)
                ],
                "graph_relation_counts": [
                    {"provider": p, "relation_type": value, "count": count}
                    for p, value, count in session.execute(graph_relation_query)
                ],
                "graph_status_counts": [
                    {"provider": p, "status": value, "count": count}
                    for p, value, count in session.execute(graph_status_query)
                ],
                "graph_geometry_counts": [
                    {"provider": p, "geometry_status": value, "count": count}
                    for p, value, count in session.execute(graph_geometry_query)
                ],
                "graph_role_counts": [
                    {"provider": p, "structural_role": value, "count": count}
                    for p, value, count in session.execute(graph_role_query)
                ],
            }


def _graph_count_query(column):
    return (
        select(StructuralEdge.source, column, func.count(StructuralEdge.id))
        .where(StructuralEdge.status.in_(GRAPH_EDGE_STATUSES))
        .group_by(StructuralEdge.source, column)
        .order_by(StructuralEdge.source, column)
    )


def _matched_record_subquery(*, source_id: int | None = None):
    query = select(
        HierarchyMatchCandidate.provider.label("provider"),
        HierarchyMatchCandidate.record_id.label("record_id"),
        func.count(HierarchyMatchCandidate.id).label("candidate_count"),
    ).join(
        HierarchyRecord,
        HierarchyRecord.id == HierarchyMatchCandidate.record_id,
    )
    if source_id is not None:
        query = query.where(HierarchyRecord.source_id == source_id)
    return query.group_by(
        HierarchyMatchCandidate.provider,
        HierarchyMatchCandidate.record_id,
    ).subquery()


def _matched_record_query(matched_subquery):
    return (
        select(
            matched_subquery.c.provider,
            func.count(matched_subquery.c.record_id),
            func.sum((matched_subquery.c.candidate_count > 1).cast(Integer)),
        )
        .group_by(matched_subquery.c.provider)
        .order_by(matched_subquery.c.provider)
    )
