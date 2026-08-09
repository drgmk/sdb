from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


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


class ExportRun(Base):
    __tablename__ = "export_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    selection_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True,
    )
    selection_value: Mapped[str | None] = mapped_column(
        String(200), index=True,
    )
    output_dir: Mapped[str] = mapped_column(Text, nullable=False)
    database_revision: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExportItem(Base):
    __tablename__ = "export_items"
    __table_args__ = (UniqueConstraint("run_id", "target_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("export_runs.id"), nullable=False, index=True,
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id"), nullable=False, index=True,
    )
    package_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    output_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
