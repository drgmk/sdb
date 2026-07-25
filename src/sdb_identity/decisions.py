"""Shared validation helpers for audited, append-only decision verbs.

Many operator decisions (photometry include/exclude, dataset associate, hierarchy
overrides, target lifecycle changes, catalog match overrides, …) require an
actor and reason and validate the same way. These helpers centralize that
boilerplate so every decision path raises identical, consistent errors.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os


def configured_actor(actor: str | None = None) -> str:
    """Resolve an explicit actor or the process-wide SDB_ACTOR default."""
    clean = str(actor or "").strip() or os.environ.get("SDB_ACTOR", "").strip()
    if not clean:
        raise ValueError("actor is required; pass --actor or set SDB_ACTOR")
    return clean


def resolve_reason(reason: str | None, suggested_reason: str) -> str:
    """Use an explicit reason when supplied, otherwise a contextual suggestion."""
    clean = str(reason or "").strip()
    suggestion = str(suggested_reason).strip()
    if clean:
        return clean
    if not suggestion:
        raise ValueError("reason is required")
    return suggestion


@dataclass(frozen=True)
class DecisionContext:
    """Normalized audit metadata for one operator decision."""

    actor: str
    reason: str

    @classmethod
    def resolve(
        cls,
        *,
        actor: str | None,
        reason: str | None,
        suggested_reason: str,
    ) -> "DecisionContext":
        return cls(
            actor=configured_actor(actor),
            reason=resolve_reason(reason, suggested_reason),
        )


def validate_actor_reason(actor: str | None, reason: str | None) -> tuple[str, str]:
    """Validate and normalize an audited decision's actor and reason.

    Returns the stripped ``(actor, reason)``; raises ``ValueError`` if either is
    empty after stripping.
    """
    clean_actor = str(actor or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_actor or not clean_reason:
        raise ValueError("actor and reason are required")
    return clean_actor, clean_reason


def validate_enum_field(value: str, allowed: Iterable[str], field_name: str) -> str:
    """Validate and normalize an enum-style field (lower-cased, stripped)."""
    clean = value.strip().lower()
    allowed_set = set(allowed)
    if clean not in allowed_set:
        raise ValueError(f"{field_name} must be one of {sorted(allowed_set)}")
    return clean
