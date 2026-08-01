from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditedActionMixin, Base, utcnow


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
