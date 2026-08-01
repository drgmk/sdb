from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditedActionMixin, Base, utcnow


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


class UserNote(Base):
    __tablename__ = "user_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
