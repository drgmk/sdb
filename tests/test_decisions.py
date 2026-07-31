from __future__ import annotations

import pytest

from sdb_identity.decisions import (
    DecisionContext,
    configured_actor,
    resolve_reason,
    validate_actor_reason,
)


def test_validate_actor_reason_strips_and_returns():
    assert validate_actor_reason("  grant ", " looks right ") == ("grant", "looks right")


@pytest.mark.parametrize("actor, reason", [("", "r"), ("a", ""), ("  ", "r"), ("a", "  ")])
def test_validate_actor_reason_requires_both(actor, reason):
    with pytest.raises(ValueError, match="actor and reason are required"):
        validate_actor_reason(actor, reason)


def test_decision_context_uses_configured_actor_and_suggested_reason(monkeypatch):
    monkeypatch.setenv("SDB_ACTOR", " grant ")
    decision = DecisionContext.resolve(
        actor=None,
        reason=None,
        suggested_reason="Excluded allwise W3 after blend review",
    )
    assert decision.actor == "grant"
    assert decision.reason == "Excluded allwise W3 after blend review"


def test_explicit_decision_metadata_overrides_defaults(monkeypatch):
    monkeypatch.setenv("SDB_ACTOR", "default")
    assert configured_actor(" reviewer ") == "reviewer"
    assert resolve_reason(" inspected image ", "automatic text") == "inspected image"
