from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .decisions import DecisionContext
from .models import Sample, SampleMembershipAction, Target
from .targets import resolve_target


@dataclass(frozen=True)
class SampleSummary:
    id: int
    name: str
    sample_date: date | None
    note: str | None
    member_count: int


class SampleService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.sessions = session_factory

    def create(
        self, name: str, *, sample_date: str | date | None = None,
        note: str | None = None,
    ) -> Sample:
        clean = self._name(name)
        parsed_date = self._date(sample_date)
        with self.sessions.begin() as session:
            if session.scalar(select(Sample.id).where(Sample.name == clean)):
                raise ValueError(f"sample already exists: {clean}")
            value = Sample(name=clean, sample_date=parsed_date, note=self._note(note))
            session.add(value)
            session.flush()
            return value

    def set_metadata(
        self, name: str, *, sample_date: str | date | None = None,
        note: str | None = None,
    ) -> Sample:
        with self.sessions.begin() as session:
            sample = self._sample(session, name)
            sample.sample_date = self._date(sample_date)
            sample.note = self._note(note)
            sample.updated_at = datetime.now(timezone.utc)
            session.flush()
            return sample

    def list(self) -> list[SampleSummary]:
        with self.sessions() as session:
            counts = dict(session.execute(
                select(
                    SampleMembershipAction.sample_id,
                    func.count(SampleMembershipAction.id),
                )
                .where(SampleMembershipAction.id.in_(
                    select(func.max(SampleMembershipAction.id)).group_by(
                        SampleMembershipAction.sample_id,
                        SampleMembershipAction.target_id,
                    )
                ))
                .where(SampleMembershipAction.action == "add")
                .group_by(SampleMembershipAction.sample_id)
            ).all())
            return [
                SampleSummary(
                    value.id, value.name, value.sample_date, value.note,
                    counts.get(value.id, 0),
                )
                for value in session.scalars(select(Sample).order_by(Sample.name))
            ]

    def add(
        self, name: str, target_reference: str | int, *,
        actor: str | None, reason: str | None = None,
    ):
        return self._action(name, target_reference, "add", actor=actor, reason=reason)

    def remove(
        self, name: str, target_reference: str | int, *,
        actor: str | None, reason: str | None = None,
    ):
        return self._action(name, target_reference, "remove", actor=actor, reason=reason)

    def members(self, name: str) -> list[Target]:
        with self.sessions() as session:
            sample = self._sample(session, name)
            latest = (
                select(
                    SampleMembershipAction.target_id,
                    func.max(SampleMembershipAction.id).label("action_id"),
                )
                .where(SampleMembershipAction.sample_id == sample.id)
                .group_by(SampleMembershipAction.target_id)
                .subquery()
            )
            return list(session.scalars(
                select(Target)
                .join(latest, latest.c.target_id == Target.id)
                .join(
                    SampleMembershipAction,
                    SampleMembershipAction.id == latest.c.action_id,
                )
                .where(SampleMembershipAction.action == "add")
                .order_by(Target.sdbid)
            ))

    def import_members(
        self, name: str, path: str | Path, *,
        actor: str | None, reason: str | None = None,
    ) -> dict[str, int]:
        path = Path(path)
        text = path.read_text(encoding="utf-8-sig")
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
        references = []
        for number, row in enumerate(rows, start=2):
            reference = next((
                str(row.get(key) or "").strip()
                for key in ("target", "sdbid", "name")
                if str(row.get(key) or "").strip()
            ), "")
            if not reference:
                raise ValueError(f"row {number} has no target, sdbid, or name")
            references.append(reference)
        with self.sessions.begin() as session:
            sample = self._sample(session, name)
            targets = []
            for reference in references:
                target = resolve_target(session, reference)
                if target is None:
                    raise KeyError(f"target not found: {reference}")
                targets.append(target)
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Imported {len({target.id for target in targets})} "
                    f"members into sample {sample.name} from {path.name}"
                ),
            )
            added = skipped = 0
            unique_targets = {target.id: target for target in targets}.values()
            for target in unique_targets:
                if self._current_action(session, sample.id, target.id) == "add":
                    skipped += 1
                    continue
                session.add(SampleMembershipAction(
                    sample_id=sample.id, target_id=target.id, action="add",
                    actor=decision.actor, reason=decision.reason,
                ))
                added += 1
            return {"rows": len(rows), "added": added, "skipped": skipped}

    def _action(self, name, target_reference, action, *, actor, reason):
        with self.sessions.begin() as session:
            sample = self._sample(session, name)
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            current = self._current_action(session, sample.id, target.id)
            if current == action or (current is None and action == "remove"):
                state = "member" if action == "add" else "not a member"
                raise ValueError(f"target is already {state} of sample {sample.name}")
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"{'Added' if action == 'add' else 'Removed'} "
                    f"{target.sdbid} {'to' if action == 'add' else 'from'} "
                    f"sample {sample.name}"
                ),
            )
            value = SampleMembershipAction(
                sample_id=sample.id, target_id=target.id, action=action,
                actor=decision.actor, reason=decision.reason,
            )
            session.add(value)
            session.flush()
            return value

    @staticmethod
    def _current_action(session, sample_id, target_id):
        return session.scalar(
            select(SampleMembershipAction.action)
            .where(
                SampleMembershipAction.sample_id == sample_id,
                SampleMembershipAction.target_id == target_id,
            )
            .order_by(SampleMembershipAction.id.desc())
            .limit(1)
        )

    @staticmethod
    def _sample(session, name):
        clean = SampleService._name(name)
        value = session.scalar(select(Sample).where(Sample.name == clean))
        if value is None:
            raise KeyError(f"sample not found: {clean}")
        return value

    @staticmethod
    def _name(value):
        clean = str(value).strip()
        if not clean:
            raise ValueError("sample name is required")
        return clean

    @staticmethod
    def _date(value):
        if value is None or value == "":
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as error:
            raise ValueError("sample date must be YYYY-MM-DD") from error

    @staticmethod
    def _note(value):
        if value is None:
            return None
        clean = str(value).strip()
        return clean or None
