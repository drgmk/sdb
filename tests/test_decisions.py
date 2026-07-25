from __future__ import annotations

import pytest

from sdb_identity.decisions import validate_actor_reason, validate_enum_field


def test_validate_actor_reason_strips_and_returns():
    assert validate_actor_reason("  grant ", " looks right ") == ("grant", "looks right")


@pytest.mark.parametrize("actor, reason", [("", "r"), ("a", ""), ("  ", "r"), ("a", "  ")])
def test_validate_actor_reason_requires_both(actor, reason):
    with pytest.raises(ValueError, match="actor and reason are required"):
        validate_actor_reason(actor, reason)


def test_validate_enum_field_normalizes():
    assert validate_enum_field(" Physical ", {"physical", "composite"}, "role") == "physical"


def test_validate_enum_field_rejects_unknown():
    with pytest.raises(ValueError, match=r"role must be one of \['composite', 'physical'\]"):
        validate_enum_field("bogus", {"physical", "composite"}, "role")
