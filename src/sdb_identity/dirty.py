from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .models.exports import ExportDirtyTarget
from .models.samples import Sample, SampleMembershipAction
from .models.identity import Target


def mark_export_dirty(
    session: Session,
    target_id: int,
    *,
    source_type: str,
    source_id: str | int | None,
    reason: str,
) -> ExportDirtyTarget:
    value = ExportDirtyTarget(
        target_id=target_id,
        source_type=source_type,
        source_id=None if source_id is None else str(source_id),
        reason=reason,
    )
    session.add(value)
    return value


def export_dirty_watermark(
    session: Session,
    target_id: int,
) -> int | None:
    """Return the newest pending event visible in the current read snapshot."""

    return session.scalar(
        select(func.max(ExportDirtyTarget.id)).where(
            ExportDirtyTarget.target_id == target_id,
            ExportDirtyTarget.exported_at.is_(None),
        )
    )


def mark_exported_through(
    session: Session,
    target_id: int,
    event_id: int,
) -> int:
    """Acknowledge pending events no newer than an exported read snapshot."""

    result = session.execute(
        update(ExportDirtyTarget)
        .where(
            ExportDirtyTarget.target_id == target_id,
            ExportDirtyTarget.exported_at.is_(None),
            ExportDirtyTarget.id <= event_id,
        )
        .values(exported_at=datetime.now(timezone.utc))
    )
    return result.rowcount


def clear_export_dirty(session: Session, target_id: int) -> int:
    """Explicitly acknowledge every event currently visible to this session."""

    watermark = export_dirty_watermark(session, target_id)
    if watermark is None:
        return 0
    return mark_exported_through(session, target_id, watermark)


def pending_export_targets(
    session_factory: sessionmaker[Session],
    *,
    sample: str | None = None,
) -> list[tuple[Target, int, datetime]]:
    with session_factory() as session:
        query = (
            select(
                Target,
                ExportDirtyTarget.id,
                ExportDirtyTarget.created_at,
            )
            .join(ExportDirtyTarget, ExportDirtyTarget.target_id == Target.id)
            .where(ExportDirtyTarget.exported_at.is_(None))
            .order_by(Target.id, ExportDirtyTarget.id)
        )
        if sample is not None:
            sample_id = session.scalar(select(Sample.id).where(Sample.name == sample))
            if sample_id is None:
                raise KeyError(f"sample not found: {sample}")
            latest = (
                select(
                    SampleMembershipAction.target_id,
                    func.max(SampleMembershipAction.id).label("action_id"),
                )
                .where(SampleMembershipAction.sample_id == sample_id)
                .group_by(SampleMembershipAction.target_id)
                .subquery()
            )
            current_ids = select(latest.c.target_id).join(
                SampleMembershipAction,
                SampleMembershipAction.id == latest.c.action_id,
            ).where(SampleMembershipAction.action == "add")
            query = query.where(Target.id.in_(current_ids))
        rows = session.execute(query)
        grouped: dict[int, tuple[Target, int, datetime]] = {}
        for target, _event_id, created_at in rows:
            previous = grouped.get(target.id)
            if previous is None:
                grouped[target.id] = (target, 1, created_at)
            else:
                grouped[target.id] = (
                    target,
                    previous[1] + 1,
                    min(previous[2], created_at),
                )
        return list(grouped.values())
