from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditedActionMixin, Base, utcnow


class CatalogBatchRequest(Base):
    __tablename__ = "catalog_batch_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    release: Mapped[str] = mapped_column(String(100), nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CatalogRun(Base):
    __tablename__ = "catalog_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    batch_request_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_batch_requests.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    release: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    query_ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    query_dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    query_epoch: Mapped[float] = mapped_column(Float, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    selected_source_id: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CatalogDetection(Base):
    """One provider-native object or observation within one catalog release."""

    __tablename__ = "catalog_detections"
    __table_args__ = (UniqueConstraint("provider", "release", "detection_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    release: Mapped[str] = mapped_column(String(100), nullable=False)
    detection_key: Mapped[str] = mapped_column(String(240), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float] = mapped_column(Float, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    normalization_status: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending",
        nullable=False, index=True,
    )
    normalization_error: Mapped[str | None] = mapped_column(Text)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CatalogDetectionProvenance(Base):
    """One source catalogue/table encounter contributing to a detection."""

    __tablename__ = "catalog_detection_provenance"
    __table_args__ = (UniqueConstraint("detection_id", "provenance_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    detection_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_detections.id"), nullable=False, index=True
    )
    provenance_key: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    service: Mapped[str | None] = mapped_column(String(80))
    catalog_id: Mapped[str | None] = mapped_column(String(160))
    table_id: Mapped[str | None] = mapped_column(String(200))
    row_key: Mapped[str | None] = mapped_column(String(300))
    identifier_column: Mapped[str | None] = mapped_column(String(160))
    identifier_value: Mapped[str | None] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(Text)
    access_url: Mapped[str | None] = mapped_column(Text)
    readme_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class RawCatalogRow(Base):
    __tablename__ = "raw_catalog_rows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("catalog_runs.id"), nullable=False, index=True)
    detection_id: Mapped[int] = mapped_column(ForeignKey("catalog_detections.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float] = mapped_column(Float, nullable=False)
    separation_arcsec: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)


class NormalizedMeasurement(Base):
    """Provider values keyed by detection, with immutable first-seen provenance.

    ``run_id``, ``target_id``, and ``raw_row_id`` identify the encounter that
    first normalized this canonical row. They are not current ownership or
    association state; those are derived from current encounters and explicit
    measurement-target associations.
    """

    __tablename__ = "normalized_measurements"
    __table_args__ = (
        UniqueConstraint("detection_id", "measurement_key"),
        Index(
            "ix_normalized_measurements_provider_source",
            "provider",
            "source_id",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("catalog_runs.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    raw_row_id: Mapped[int] = mapped_column(ForeignKey("raw_catalog_rows.id"), nullable=False)
    detection_id: Mapped[int] = mapped_column(ForeignKey("catalog_detections.id"), nullable=False, index=True)
    measurement_key: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    band: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    error: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    systematic_error: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    upper_limit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    bibcode: Mapped[str] = mapped_column(String(30), nullable=False)
    quality: Mapped[str | None] = mapped_column(String(100))
    note1: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    note2: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    resolution_major_arcsec: Mapped[float | None] = mapped_column(Float)
    resolution_minor_arcsec: Mapped[float | None] = mapped_column(Float)
    resolution_kind: Mapped[str | None] = mapped_column(String(40))
    resolution_reference: Mapped[str | None] = mapped_column(Text)
    ownership_scope: Mapped[str] = mapped_column(String(20), default="component", nullable=False)
    blend_state: Mapped[str] = mapped_column(String(30), default="clear", nullable=False)
    blend_reason: Mapped[str | None] = mapped_column(String(80))

    @property
    def first_seen_run_id(self) -> int:
        return self.run_id

    @property
    def first_seen_target_id(self) -> int:
        return self.target_id

    @property
    def first_seen_raw_row_id(self) -> int:
        return self.raw_row_id


class IrasSourceFamily(Base):
    """One confidently identified astrophysical IRAS PSC/FSC source pair."""

    __tablename__ = "iras_source_families"
    __table_args__ = (
        UniqueConstraint("psc_detection_id"),
        UniqueConstraint("fsc_detection_id"),
        UniqueConstraint("psc_detection_id", "fsc_detection_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    psc_detection_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_detections.id"), nullable=False, index=True,
    )
    fsc_detection_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_detections.id"), nullable=False, index=True,
    )
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_separation: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )


class IrasDetectionFamily(Base):
    """Target-scoped reconciliation of PSC/FSC catalog-run results."""

    __tablename__ = "iras_detection_families"
    __table_args__ = (UniqueConstraint("psc_run_id", "fsc_run_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    psc_run_id: Mapped[int] = mapped_column(ForeignKey("catalog_runs.id"), nullable=False)
    fsc_run_id: Mapped[int] = mapped_column(ForeignKey("catalog_runs.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    normalized_separation: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    source_family_id: Mapped[int | None] = mapped_column(
        ForeignKey("iras_source_families.id"), index=True,
    )


class IrasBandSelection(Base):
    __tablename__ = "iras_band_selections"
    __table_args__ = (UniqueConstraint("family_id", "band"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("iras_source_families.id"), nullable=False, index=True)
    band: Mapped[str] = mapped_column(String(30), nullable=False)
    selected_measurement_id: Mapped[int] = mapped_column(ForeignKey("normalized_measurements.id"), nullable=False)
    alternate_measurement_id: Mapped[int] = mapped_column(ForeignKey("normalized_measurements.id"), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class CatalogResultDecision(AuditedActionMixin, Base):
    """Append-only interpretation of one immutable acquisition run."""

    __tablename__ = "catalog_result_decisions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('accept_detection', 'reviewed_no_match')",
            name="ck_catalog_result_decision_action",
        ),
        CheckConstraint(
            "(action = 'accept_detection' AND accepted_detection_id IS NOT NULL "
            "AND reviewed_raw_row_id IS NOT NULL) OR "
            "(action = 'reviewed_no_match' AND accepted_detection_id IS NULL "
            "AND reviewed_raw_row_id IS NULL)",
            name="ck_catalog_result_decision_evidence",
        ),
        Index(
            "ix_catalog_result_decisions_run_order",
            "reviewed_run_id",
            "id",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    reviewed_run_id: Mapped[int] = mapped_column(ForeignKey("catalog_runs.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    accepted_detection_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_detections.id"), index=True)
    reviewed_raw_row_id: Mapped[int | None] = mapped_column(ForeignKey("raw_catalog_rows.id"), index=True)


class CatalogRetryAction(AuditedActionMixin, Base):
    """Operator request linking a failed run to its new acquisition attempt."""

    __tablename__ = "catalog_retry_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    failed_run_id: Mapped[int] = mapped_column(ForeignKey("catalog_runs.id"), nullable=False, index=True)
    retry_run_id: Mapped[int] = mapped_column(ForeignKey("catalog_runs.id"), nullable=False, index=True)


class CatalogTargetAssociationAction(AuditedActionMixin, Base):
    """Append-only operator decision linking one catalog detection to a target."""

    __tablename__ = "catalog_target_association_actions"
    __table_args__ = (
        Index(
            "ix_catalog_target_association_actions_pair",
            "target_id",
            "detection_id",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id"), nullable=False, index=True,
    )
    detection_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_detections.id"), nullable=False, index=True,
    )
    action: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True,
    )
    method: Mapped[str] = mapped_column(
        String(40), default="manual_review", nullable=False,
    )
    reviewed_run_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_runs.id"), nullable=False, index=True,
    )
    reviewed_raw_row_id: Mapped[int] = mapped_column(
        ForeignKey("raw_catalog_rows.id"), nullable=False, index=True,
    )
    family_action_id: Mapped[int | None] = mapped_column(
        ForeignKey("iras_family_target_association_actions.id"), index=True,
    )


class IrasFamilyTargetAssociationAction(AuditedActionMixin, Base):
    """One operator decision applying to both detections in an IRAS family."""

    __tablename__ = "iras_family_target_association_actions"
    __table_args__ = (
        Index(
            "ix_iras_family_target_association_actions_pair",
            "target_id",
            "family_id",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("iras_source_families.id"), nullable=False, index=True,
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id"), nullable=False, index=True,
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reviewed_run_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_runs.id"), nullable=False, index=True,
    )
    reviewed_raw_row_id: Mapped[int] = mapped_column(
        ForeignKey("raw_catalog_rows.id"), nullable=False, index=True,
    )


class CatalogAttribute(Base):
    __tablename__ = "catalog_attributes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("catalog_runs.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    raw_row_id: Mapped[int] = mapped_column(ForeignKey("raw_catalog_rows.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_float: Mapped[float | None] = mapped_column(Float)
    uncertainty: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    quality: Mapped[str | None] = mapped_column(String(100))
    reference: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
