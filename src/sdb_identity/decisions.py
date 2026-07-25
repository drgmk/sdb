"""Shared validation helpers for audited, append-only decision verbs.

Many operator decisions (photometry include/exclude, dataset associate, hierarchy
overrides, target lifecycle changes, catalog match overrides, …) require an
actor and reason and validate the same way. These helpers centralize that
boilerplate so every decision path raises identical, consistent errors.
"""

from __future__ import annotations

from collections.abc import Iterable


def validate_actor_reason(actor: str, reason: str) -> tuple[str, str]:
    """Validate and normalize an audited decision's actor and reason.

    Returns the stripped ``(actor, reason)``; raises ``ValueError`` if either is
    empty after stripping.
    """
    clean_actor = actor.strip()
    clean_reason = reason.strip()
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
