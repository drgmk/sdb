from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditedActionMixin, Base, utcnow


class HierarchySource(Base):
    __tablename__ = "hierarchy_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    release: Mapped[str] = mapped_column(String(100), nullable=False)
    source_file: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(128))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class HierarchyRecord(Base):
    __tablename__ = "hierarchy_records"
    __table_args__ = (
        Index("ix_hierarchy_records_source_provider_native", "source_id", "provider", "native_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("hierarchy_sources.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    native_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    component: Mapped[str | None] = mapped_column(String(40), index=True)
    discoverer_id: Mapped[str | None] = mapped_column(String(100), index=True)
    ra_deg: Mapped[float | None] = mapped_column(Float)
    dec_deg: Mapped[float | None] = mapped_column(Float)
    first_epoch: Mapped[float | None] = mapped_column(Float)
    last_epoch: Mapped[float | None] = mapped_column(Float)
    measure_epoch: Mapped[float | None] = mapped_column(Float)
    separation_arcsec: Mapped[float | None] = mapped_column(Float)
    pa_deg: Mapped[float | None] = mapped_column(Float)
    magnitude_primary: Mapped[float | None] = mapped_column(Float)
    magnitude_secondary: Mapped[float | None] = mapped_column(Float)
    delta_mag: Mapped[float | None] = mapped_column(Float)
    raw_payload_json: Mapped[str] = mapped_column(Text, nullable=False)


class HierarchyMatchCandidate(Base):
    __tablename__ = "hierarchy_match_candidates"
    __table_args__ = (UniqueConstraint("record_id", "target_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("hierarchy_records.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    match_method: Mapped[str] = mapped_column(String(80), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    separation_arcsec: Mapped[float | None] = mapped_column(Float)
    identifier: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="candidate",
        server_default="candidate", index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class HierarchyMatchAction(AuditedActionMixin, Base):
    __tablename__ = "hierarchy_match_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("hierarchy_match_candidates.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    previous_status: Mapped[str] = mapped_column(String(30), nullable=False)
    new_status: Mapped[str] = mapped_column(String(30), nullable=False)
    system_id: Mapped[int | None] = mapped_column(ForeignKey("target_systems.id"))
    relationship_id: Mapped[int | None] = mapped_column(ForeignKey("structural_edges.id"))


class TargetSystem(Base):
    __tablename__ = "target_systems"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    primary_target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"))
    source: Mapped[str] = mapped_column(
        String(40), nullable=False, default="manual", server_default="manual",
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TargetLifecycleAction(AuditedActionMixin, Base):
    __tablename__ = "target_lifecycle_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    superseded_by_target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"), index=True)


class TargetSystemMember(Base):
    __tablename__ = "target_system_members"
    __table_args__ = (UniqueConstraint("system_id", "target_id", "component_label"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("target_systems.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    component_label: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(
        String(40), nullable=False, default="manual", server_default="manual",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StructuralEdge(Base):
    """Unified current-state structural relationship between two components/targets.

    Replaces HierarchyGraphEdge (provider-derived, re-derivable WDS geometry) and
    TargetRelationship (target-resolved accepted/manual assertions). Endpoints carry
    both a resolved target FK and a provider component label, because a provider-derived
    edge exists (keyed by native_id + component) before its targets are matched.
    Derived WDS rows (status derived/stale) are re-derivable; accepted/manual rows
    survive re-derivation. Decision history lives in StructuralEdgeAction.
    """
    __tablename__ = "structural_edges"
    __table_args__ = (
        Index("ix_structural_edges_source_native", "source", "native_id"),
        Index("ix_structural_edges_source_id", "source_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # provenance
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)  # wds|ccdm|simbad|manual
    source_id: Mapped[int | None] = mapped_column(ForeignKey("hierarchy_sources.id"), index=True)
    record_id: Mapped[int | None] = mapped_column(ForeignKey("hierarchy_records.id"), index=True)
    native_id: Mapped[str | None] = mapped_column(String(200), index=True)
    system_id: Mapped[int | None] = mapped_column(ForeignKey("target_systems.id"), index=True)
    # endpoints: resolved target FK and/or provider component label
    endpoint_a_target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"), index=True)
    endpoint_b_target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"), index=True)
    reference_label: Mapped[str | None] = mapped_column(String(80), index=True)   # endpoint A label
    component_label: Mapped[str | None] = mapped_column(String(80), index=True)   # endpoint B label
    source_component: Mapped[str | None] = mapped_column(String(80), index=True)  # raw provider component
    # semantics
    direction: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pair", server_default="pair",
    )  # pair|a_parent_b|b_parent_a
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    structural_role: Mapped[str] = mapped_column(
        String(40), nullable=False, default="non_structural",
        server_default="non_structural", index=True,
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="derived",
        server_default="derived", index=True,
    )  # derived|accepted|rejected|stale
    confidence: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unknown", server_default="unknown",
    )
    # geometry (provider-derived, nullable)
    geometry_status: Mapped[str | None] = mapped_column(String(30), index=True)
    start_ra_deg: Mapped[float | None] = mapped_column(Float)
    start_dec_deg: Mapped[float | None] = mapped_column(Float)
    end_ra_deg: Mapped[float | None] = mapped_column(Float)
    end_dec_deg: Mapped[float | None] = mapped_column(Float)
    separation_arcsec: Mapped[float | None] = mapped_column(Float)
    pa_deg: Mapped[float | None] = mapped_column(Float)
    relation_epoch: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    actor: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StructuralEdgeAction(AuditedActionMixin, Base):
    """Append-only decision/audit log for structural edges (replaces HierarchyGraphOverride)."""
    __tablename__ = "structural_edge_actions"
    __table_args__ = (
        Index("ix_structural_edge_actions_source_native", "source", "native_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edge_id: Mapped[int | None] = mapped_column(ForeignKey("structural_edges.id"), index=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    native_id: Mapped[str | None] = mapped_column(String(200), index=True)
    reference_label: Mapped[str | None] = mapped_column(String(80), index=True)
    component_label: Mapped[str | None] = mapped_column(String(80), index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)  # accept|reject|amend|manual_add|derive_refresh
    previous_status: Mapped[str | None] = mapped_column(String(30))
    new_status: Mapped[str | None] = mapped_column(String(30))
    previous_relation_type: Mapped[str | None] = mapped_column(String(40))
    new_relation_type: Mapped[str | None] = mapped_column(String(40))
    previous_structural_role: Mapped[str | None] = mapped_column(String(40))
    new_structural_role: Mapped[str | None] = mapped_column(String(40))
