from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AuditedActionMixin:
    """Shared columns for append-only operator actions."""

    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Target(Base):
    __tablename__ = "targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sdbid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    ra2000_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec2000_deg: Mapped[float] = mapped_column(Float, nullable=False)
    canonical_astrometry_id: Mapped[int | None] = mapped_column(ForeignKey("astrometric_solutions.id", use_alter=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"))
    input_name: Mapped[str | None] = mapped_column(String(200))
    input_ra_deg: Mapped[float | None] = mapped_column(Float)
    input_dec_deg: Mapped[float | None] = mapped_column(Float)
    input_epoch: Mapped[float | None] = mapped_column(Float)
    command: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AstrometricSolution(Base):
    __tablename__ = "astrometric_solutions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(100))
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float] = mapped_column(Float, nullable=False)
    pm_ra_cosdec_masyr: Mapped[float | None] = mapped_column(Float)
    pm_dec_masyr: Mapped[float | None] = mapped_column(Float)
    proper_motion_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    parallax_mas: Mapped[float | None] = mapped_column(Float)
    radial_velocity_kms: Mapped[float | None] = mapped_column(Float)
    position_bibcode: Mapped[str | None] = mapped_column(String(19))
    proper_motion_bibcode: Mapped[str | None] = mapped_column(String(19))
    parallax_bibcode: Mapped[str | None] = mapped_column(String(19))
    radial_velocity_bibcode: Mapped[str | None] = mapped_column(String(19))
    derived_ra2000_deg: Mapped[float] = mapped_column(Float, nullable=False)
    derived_dec2000_deg: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ExternalIdentifier(Base):
    __tablename__ = "external_identifiers"
    __table_args__ = (UniqueConstraint("target_id", "normalized_value"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False)
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)


class Sample(Base):
    __tablename__ = "samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    sample_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SampleMembershipAction(AuditedActionMixin, Base):
    __tablename__ = "sample_membership_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(10), nullable=False)


class SampleExportRun(Base):
    __tablename__ = "sample_export_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), nullable=False, index=True)
    output_dir: Mapped[str] = mapped_column(Text, nullable=False)
    database_revision: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SampleExportItem(Base):
    __tablename__ = "sample_export_items"
    __table_args__ = (UniqueConstraint("run_id", "target_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sample_export_runs.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    output_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AlmaSyncRun(Base):
    __tablename__ = "alma_sync_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    archive_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    watermark_before: Mapped[str | None] = mapped_column(String(40))
    watermark_after: Mapped[str | None] = mapped_column(String(40))
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    upserted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deactivated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlmaSyncChunk(Base):
    __tablename__ = "alma_sync_chunks"
    __table_args__ = (UniqueConstraint("run_id", "label"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("alma_sync_runs.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    start_mjd: Mapped[float | None] = mapped_column(Float)
    end_mjd: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archive_url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlmaMember(Base):
    __tablename__ = "alma_members"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "member_ous_uid",
            name="uq_alma_members_proposal_member_ous",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_ous_uid: Mapped[str] = mapped_column(String(200), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_names_json: Mapped[str] = mapped_column(Text, nullable=False)
    center_ra_deg: Mapped[float | None] = mapped_column(Float)
    center_dec_deg: Mapped[float | None] = mapped_column(Float, index=True)
    bounding_radius_deg: Mapped[float | None] = mapped_column(Float)
    t_min_mjd: Mapped[float | None] = mapped_column(Float, index=True)
    t_max_mjd: Mapped[float | None] = mapped_column(Float)
    release_date: Mapped[str | None] = mapped_column(String(40))
    data_rights: Mapped[str | None] = mapped_column(String(40))
    band_list: Mapped[str | None] = mapped_column(String(100), index=True)
    last_modified: Mapped[str | None] = mapped_column(String(40), index=True)
    first_seen_run_id: Mapped[int] = mapped_column(ForeignKey("alma_sync_runs.id"), nullable=False)
    last_seen_run_id: Mapped[int] = mapped_column(ForeignKey("alma_sync_runs.id"), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)


class AlmaMemberPosition(Base):
    __tablename__ = "alma_member_positions"
    __table_args__ = (UniqueConstraint("member_id", "position_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("alma_members.id"), nullable=False, index=True)
    position_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    fov_deg: Mapped[float | None] = mapped_column(Float, index=True)
    region: Mapped[str | None] = mapped_column(Text)


class ProviderOutcome(Base):
    __tablename__ = "provider_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MatchCandidate(Base):
    __tablename__ = "match_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float] = mapped_column(Float, nullable=False)
    pm_ra_cosdec_masyr: Mapped[float | None] = mapped_column(Float)
    pm_dec_masyr: Mapped[float | None] = mapped_column(Float)
    proper_motion_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    parallax_mas: Mapped[float | None] = mapped_column(Float)
    radial_velocity_kms: Mapped[float | None] = mapped_column(Float)
    position_bibcode: Mapped[str | None] = mapped_column(String(30))
    proper_motion_bibcode: Mapped[str | None] = mapped_column(String(30))
    parallax_bibcode: Mapped[str | None] = mapped_column(String(30))
    radial_velocity_bibcode: Mapped[str | None] = mapped_column(String(30))
    separation_arcsec: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    score_details: Mapped[str] = mapped_column(Text, nullable=False)
class MatchDecision(Base):
    __tablename__ = "match_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("match_candidates.id"), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


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
        String(30), default="pending", nullable=False, index=True,
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
    __table_args__ = (UniqueConstraint("detection_id", "measurement_key"),)
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


class IrasDetectionFamily(Base):
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


class IrasBandSelection(Base):
    __tablename__ = "iras_band_selections"
    __table_args__ = (UniqueConstraint("family_id", "band"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("iras_detection_families.id"), nullable=False, index=True)
    band: Mapped[str] = mapped_column(String(30), nullable=False)
    selected_measurement_id: Mapped[int] = mapped_column(ForeignKey("normalized_measurements.id"), nullable=False)
    alternate_measurement_id: Mapped[int] = mapped_column(ForeignKey("normalized_measurements.id"), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class MetadataRun(Base):
    __tablename__ = "metadata_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    release: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    query_identifier: Mapped[str | None] = mapped_column(String(200))
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    raw_response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SimbadMetadata(Base):
    __tablename__ = "simbad_metadata"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("metadata_runs.id"), unique=True, nullable=False)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    oid: Mapped[int] = mapped_column(Integer, nullable=False)
    main_id: Mapped[str] = mapped_column(String(200), nullable=False)
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    spectral_type: Mapped[str | None] = mapped_column(String(100))
    spectral_type_bibcode: Mapped[str | None] = mapped_column(String(30))
    parallax_mas: Mapped[float | None] = mapped_column(Float)
    parallax_error_mas: Mapped[float | None] = mapped_column(Float)
    parallax_bibcode: Mapped[str | None] = mapped_column(String(30))
    pm_ra_cosdec_masyr: Mapped[float | None] = mapped_column(Float)
    pm_dec_masyr: Mapped[float | None] = mapped_column(Float)
    proper_motion_bibcode: Mapped[str | None] = mapped_column(String(30))
    radial_velocity_kms: Mapped[float | None] = mapped_column(Float)
    radial_velocity_error_kms: Mapped[float | None] = mapped_column(Float)
    radial_velocity_bibcode: Mapped[str | None] = mapped_column(String(30))
    primary_object_type: Mapped[str | None] = mapped_column(String(40))


class SimbadObjectType(Base):
    __tablename__ = "simbad_object_types"
    __table_args__ = (UniqueConstraint("run_id", "object_type"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("metadata_runs.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False)
    object_type: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class SimbadRelationship(Base):
    __tablename__ = "simbad_relationships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("metadata_runs.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    related_oid: Mapped[int] = mapped_column(Integer, nullable=False)
    related_main_id: Mapped[str] = mapped_column(String(200), nullable=False)
    related_ra_deg: Mapped[float | None] = mapped_column(Float)
    related_dec_deg: Mapped[float | None] = mapped_column(Float)
    related_object_type: Mapped[str | None] = mapped_column(String(40))
    related_object_types_json: Mapped[str | None] = mapped_column(Text)
    related_spectral_type: Mapped[str | None] = mapped_column(String(100))
    related_spectral_type_bibcode: Mapped[str | None] = mapped_column(String(30))
    membership_percent: Mapped[int | None] = mapped_column(Integer)
    link_bibcode: Mapped[str | None] = mapped_column(String(30))
    separation_arcsec: Mapped[float | None] = mapped_column(Float)


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
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="candidate", index=True)
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
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
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
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
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
    direction: Mapped[str] = mapped_column(String(20), nullable=False, default="pair")  # pair|a_parent_b|b_parent_a
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    structural_role: Mapped[str] = mapped_column(String(40), nullable=False, default="non_structural", index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="derived", index=True)  # derived|accepted|rejected|stale
    confidence: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown")
    # geometry (provider-derived, nullable)
    geometry_status: Mapped[str | None] = mapped_column(String(30), index=True)
    start_ra_deg: Mapped[float | None] = mapped_column(Float)
    start_dec_deg: Mapped[float | None] = mapped_column(Float)
    end_ra_deg: Mapped[float | None] = mapped_column(Float)
    end_dec_deg: Mapped[float | None] = mapped_column(Float)
    separation_arcsec: Mapped[float | None] = mapped_column(Float)
    pa_deg: Mapped[float | None] = mapped_column(Float)
    relation_epoch: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actor: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
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


class MeasurementTargetAssociation(Base):
    __tablename__ = "measurement_target_associations"
    __table_args__ = (UniqueConstraint("measurement_id", "target_id", "role"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    measurement_id: Mapped[int] = mapped_column(ForeignKey("normalized_measurements.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    weight: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MeasurementAssociationAction(AuditedActionMixin, Base):
    __tablename__ = "measurement_association_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    measurement_id: Mapped[int] = mapped_column(ForeignKey("normalized_measurements.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    weight: Mapped[float | None] = mapped_column(Float)


class UserNote(Base):
    __tablename__ = "user_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    delimiter: Mapped[str] = mapped_column(String(10), nullable=False)
    requested_stages_json: Mapped[str] = mapped_column(Text, nullable=False)
    workers_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportItem(Base):
    __tablename__ = "import_items"
    __table_args__ = (UniqueConstraint("run_id", "row_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("import_runs.id"), nullable=False, index=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    input_json: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (UniqueConstraint("item_id", "stage"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("import_runs.id"), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("import_items.id"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MeasurementEligibilityAction(AuditedActionMixin, Base):
    __tablename__ = "measurement_eligibility_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    measurement_id: Mapped[int] = mapped_column(
        ForeignKey("normalized_measurements.id"), nullable=False, index=True
    )
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False)


class DatasetRevision(Base):
    __tablename__ = "dataset_revisions"
    __table_args__ = (UniqueConstraint("dataset", "source_sha256"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    removed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unresolved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ambiguous_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CuratedRecord(Base):
    __tablename__ = "curated_records"
    __table_args__ = (UniqueConstraint("revision_id", "record_no"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("dataset_revisions.id"), nullable=False, index=True)
    record_no: Mapped[int] = mapped_column(Integer, nullable=False)
    row_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"), index=True)
    association_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    association_method: Mapped[str | None] = mapped_column(String(40))
    association_message: Mapped[str | None] = mapped_column(Text)


class CuratedAssociationAction(AuditedActionMixin, Base):
    __tablename__ = "curated_association_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    record_no: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"), index=True)


class ReferenceApplicationRun(Base):
    __tablename__ = "reference_application_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    target_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refreshed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ambiguous_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    no_match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unmatched_row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReferenceApplicationItem(Base):
    __tablename__ = "reference_application_items"
    __table_args__ = (UniqueConstraint("application_run_id", "target_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_run_id: Mapped[int] = mapped_column(ForeignKey("reference_application_runs.id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    catalog_run_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_runs.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    selected_source_id: Mapped[str | None] = mapped_column(String(200), index=True)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ReferenceApplicationRecord(Base):
    __tablename__ = "reference_application_records"
    __table_args__ = (UniqueConstraint("application_run_id", "source_identifier"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_run_id: Mapped[int] = mapped_column(ForeignKey("reference_application_runs.id"), nullable=False, index=True)
    source_identifier: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    row_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    candidate_target_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    selected_target_ids_json: Mapped[str] = mapped_column(Text, nullable=False)


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


class ExportDirtyTarget(Base):
    __tablename__ = "export_dirty_targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(String(100), index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
