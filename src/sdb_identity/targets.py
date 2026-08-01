"""Canonical target lookup with explicit shared-alias handling."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .identifiers import normalize_identifier
from .models.identity import ExternalIdentifier, Target


@dataclass(frozen=True)
class AmbiguousTargetReference(ValueError):
    """A non-canonical identifier belongs to more than one SDB target."""

    reference: str
    sdbids: tuple[str, ...]

    def __str__(self) -> str:
        return (
            f"target reference is ambiguous: {self.reference}; "
            f"matches {', '.join(self.sdbids)}"
        )


class TargetRepository:
    """Resolve target references without silently choosing a shared alias."""

    def __init__(self, session: Session):
        self.session = session

    def resolve_many(self, reference: str | int) -> list[Target]:
        if isinstance(reference, int) or str(reference).isdigit():
            target = self.session.get(Target, int(reference))
            return [] if target is None else [target]

        text = str(reference)
        target = self.session.scalar(
            select(Target).where(Target.sdbid == text)
        )
        if target is not None:
            return [target]

        return list(self.session.scalars(
            select(Target)
            .join(
                ExternalIdentifier,
                ExternalIdentifier.target_id == Target.id,
            )
            .where(
                ExternalIdentifier.normalized_value
                == normalize_identifier(text)
            )
            .distinct()
            .order_by(Target.sdbid)
        ))

    def resolve_one(self, reference: str | int) -> Target | None:
        targets = self.resolve_many(reference)
        if len(targets) > 1:
            raise AmbiguousTargetReference(
                str(reference),
                tuple(target.sdbid for target in targets),
            )
        return targets[0] if targets else None


def resolve_targets(
    session: Session, reference: str | int,
) -> list[Target]:
    """Resolve a reference to every matching target."""

    return TargetRepository(session).resolve_many(reference)


def resolve_target(
    session: Session, reference: str | int,
) -> Target | None:
    """Resolve exactly one target, raising on a shared or ambiguous alias."""

    return TargetRepository(session).resolve_one(reference)
