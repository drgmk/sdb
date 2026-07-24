from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    ExportDirtyTarget, ExternalIdentifier, Sample, SampleMembershipAction, Target,
)
from .service import normalize_identifier


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


def clear_export_dirty(session: Session, target_id: int) -> int:
    result = session.execute(
        update(ExportDirtyTarget)
        .where(
            ExportDirtyTarget.target_id == target_id,
            ExportDirtyTarget.exported_at.is_(None),
        )
        .values(exported_at=datetime.now(timezone.utc))
    )
    return result.rowcount


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


def resolve_targets(session: Session, reference: str | int) -> list[Target]:
    """Resolve a reference to every matching target.

    A numeric id or an sdbid resolves to at most one target. A name/alias is
    matched against external_identifiers by normalized value and may resolve to
    several targets (e.g. a shared system alias such as "HD 26965" carried by a
    primary and its components). Callers decide how to handle more than one.
    """
    if isinstance(reference, int) or str(reference).isdigit():
        target = session.get(Target, int(reference))
        return [target] if target is not None else []
    target = session.scalar(select(Target).where(Target.sdbid == str(reference)))
    if target is not None:
        return [target]
    target_ids = session.scalars(
        select(ExternalIdentifier.target_id)
        .where(ExternalIdentifier.normalized_value == normalize_identifier(str(reference)))
        .distinct()
    ).all()
    targets = (session.get(Target, tid) for tid in target_ids)
    return sorted((t for t in targets if t is not None), key=lambda t: t.sdbid)


def find_target(session: Session, reference: str | int) -> Target | None:
    targets = resolve_targets(session, reference)
    return targets[0] if targets else None
