from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditedActionMixin, Base, utcnow


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
