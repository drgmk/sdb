from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditedActionMixin, Base, utcnow


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


class MeasurementEligibilityAction(AuditedActionMixin, Base):
    __tablename__ = "measurement_eligibility_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    measurement_id: Mapped[int] = mapped_column(
        ForeignKey("normalized_measurements.id"), nullable=False, index=True
    )
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False)
