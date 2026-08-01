from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditedActionMixin, Base, utcnow


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
