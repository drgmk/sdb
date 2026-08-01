from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditedActionMixin, Base, utcnow


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
    __table_args__ = (
        UniqueConstraint("member_id", "position_key"),
        Index("ix_alma_member_positions_dec_ra", "dec_deg", "ra_deg"),
        Index(
            "ix_alma_member_positions_fov_dec_ra",
            "fov_deg",
            "dec_deg",
            "ra_deg",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("alma_members.id"), nullable=False, index=True)
    position_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    fov_deg: Mapped[float | None] = mapped_column(Float, index=True)
    region: Mapped[str | None] = mapped_column(Text)
