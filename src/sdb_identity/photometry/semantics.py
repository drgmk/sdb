"""Canonical ownership and blending vocabulary for normalized photometry."""

from __future__ import annotations


OWNERSHIP_SCOPES = frozenset({"component", "system", "shared", "ambiguous"})
BLEND_STATES = frozenset({"clear", "blended", "ambiguous", "unknown"})


def validate_photometry_semantics(
    ownership_scope: str,
    blend_state: str,
) -> tuple[str, str]:
    """Return normalized canonical values, rejecting vocabulary drift."""
    scope = str(ownership_scope).strip()
    state = str(blend_state).strip()
    if scope not in OWNERSHIP_SCOPES:
        raise ValueError(
            f"ownership_scope must be one of {sorted(OWNERSHIP_SCOPES)}; got {scope!r}"
        )
    if state not in BLEND_STATES:
        raise ValueError(
            f"blend_state must be one of {sorted(BLEND_STATES)}; got {state!r}"
        )
    return scope, state
