from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .decisions import DecisionContext
from .dirty import mark_export_dirty
from .models.identity import Target
from .models.hierarchy import TargetLifecycleAction
from .targets import resolve_target
from .vocabulary import TargetRole, TargetState


@dataclass(frozen=True)
class TargetLifecycleStatus:
    target_id: int
    sdbid: str
    role: TargetRole
    state: TargetState
    superseded_by_target_id: int | None = None
    superseded_by_sdbid: str | None = None
    action_id: int | None = None
    actor: str | None = None
    reason: str | None = None
def target_lifecycle_status(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
) -> TargetLifecycleStatus:
    with session_factory() as session:
        target = resolve_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        action = session.scalar(select(TargetLifecycleAction).where(
            TargetLifecycleAction.target_id == target.id
        ).order_by(TargetLifecycleAction.id.desc()).limit(1))
        if action is None:
            return TargetLifecycleStatus(
                target.id,
                target.sdbid,
                TargetRole.UNSPECIFIED,
                TargetState.ACTIVE,
            )
        replacement = (
            None if action.superseded_by_target_id is None
            else session.get(Target, action.superseded_by_target_id)
        )
        return TargetLifecycleStatus(
            target.id,
            target.sdbid,
            TargetRole.parse(action.role, "role"),
            TargetState.parse(action.state, "state"),
            action.superseded_by_target_id,
            None if replacement is None else replacement.sdbid,
            action.id,
            action.actor,
            action.reason,
        )


def set_target_lifecycle(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    role: str | TargetRole,
    state: str | TargetState,
    actor: str | None,
    reason: str | None = None,
    superseded_by: str | int | None = None,
) -> TargetLifecycleAction:
    role = TargetRole.parse(role, "role")
    state = TargetState.parse(state, "state")
    if state is TargetState.SUPERSEDED and superseded_by is None:
        raise ValueError("superseded state requires superseded_by")
    if state is not TargetState.SUPERSEDED and superseded_by is not None:
        raise ValueError("superseded_by is only valid for superseded state")
    with session_factory.begin() as session:
        target = resolve_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        replacement = (
            None if superseded_by is None
            else resolve_target(session, superseded_by)
        )
        if superseded_by is not None and replacement is None:
            raise KeyError(f"replacement target not found: {superseded_by}")
        if replacement is not None and replacement.id == target.id:
            raise ValueError("a target cannot supersede itself")
        decision = DecisionContext.resolve(
            actor=actor,
            reason=reason,
            suggested_reason=(
                f"Set {target.sdbid} modelling role to {role} with state {state}"
            ),
        )
        action = TargetLifecycleAction(
            target_id=target.id,
            role=role.value,
            state=state.value,
            superseded_by_target_id=None if replacement is None else replacement.id,
            actor=decision.actor,
            reason=decision.reason,
        )
        session.add(action)
        session.flush()
        mark_export_dirty(
            session,
            target.id,
            source_type="target_lifecycle",
            source_id=action.id,
            reason="target role or lifecycle state changed",
        )
        return action


def target_lifecycle_history(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
) -> list[TargetLifecycleAction]:
    with session_factory() as session:
        target = resolve_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        return list(session.scalars(select(TargetLifecycleAction).where(
            TargetLifecycleAction.target_id == target.id
        ).order_by(TargetLifecycleAction.id)))
