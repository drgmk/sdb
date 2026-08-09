"""One target-selection contract shared by batch operations."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models.hierarchy import TargetLifecycleAction
from .models.identity import Target
from .samples.service import SampleService
from .targets import resolve_target
from .vocabulary import INACTIVE_TARGET_STATES, TargetState


@dataclass(frozen=True)
class TargetSelection:
    kind: str
    value: str | None
    target_ids: tuple[int, ...]
    sdbids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "value": self.value,
            "target": self.value if self.kind == "target" else None,
            "sample": self.value if self.kind == "sample" else None,
            "all": self.kind == "all",
            "selected_sdbids": list(self.sdbids),
        }


def resolve_target_selection(
    session_factory: sessionmaker[Session],
    *,
    target_reference: str | int | None = None,
    sample: str | None = None,
    all_targets: bool = False,
) -> TargetSelection:
    """Resolve exactly one CLI-style selector against the authoritative DB."""
    if sum((target_reference is not None, sample is not None, all_targets)) != 1:
        raise ValueError("provide exactly one of TARGET, --sample, or --all")
    if sample is not None:
        targets = SampleService(session_factory).members(sample)
        return TargetSelection(
            kind="sample",
            value=sample,
            target_ids=tuple(target.id for target in targets),
            sdbids=tuple(target.sdbid for target in targets),
        )
    with session_factory() as session:
        if target_reference is not None:
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            return TargetSelection(
                kind="target",
                value=target.sdbid,
                target_ids=(target.id,),
                sdbids=(target.sdbid,),
            )
        targets = list(session.scalars(select(Target).order_by(Target.sdbid)))
        latest = _latest_lifecycle_actions(session)
        active = [
            target for target in targets
            if target.id not in latest
            or TargetState.parse(
                latest[target.id].state, "state",
            ) not in INACTIVE_TARGET_STATES
        ]
        active.sort(key=lambda target: target.sdbid)
        return TargetSelection(
            kind="all",
            value=None,
            target_ids=tuple(target.id for target in active),
            sdbids=tuple(target.sdbid for target in active),
        )


def _latest_lifecycle_actions(
    session: Session,
) -> dict[int, TargetLifecycleAction]:
    latest: dict[int, TargetLifecycleAction] = {}
    for action in session.scalars(
        select(TargetLifecycleAction).order_by(TargetLifecycleAction.id.desc())
    ):
        latest.setdefault(action.target_id, action)
    return latest
