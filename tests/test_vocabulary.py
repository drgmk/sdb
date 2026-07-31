from __future__ import annotations

import json

import pytest

from sdb_identity.vocabulary import (
    INACTIVE_TARGET_STATES,
    PROVIDER_FAILURE_STATUSES,
    MeasurementAssociationActionKind,
    MeasurementTargetRole,
    ProviderRunStatus,
    ReviewPriority,
    TargetRole,
    TargetState,
    review_priority_rank,
)


def test_domain_values_parse_normalized_strings_and_expose_cli_choices():
    assert TargetRole.parse(" Physical ", "role") is TargetRole.PHYSICAL
    assert TargetRole.choices() == ("unspecified", "physical", "composite")
    with pytest.raises(
        ValueError,
        match=r"role must be one of \['composite', 'physical', 'unspecified'\]",
    ):
        TargetRole.parse("bogus", "role")


def test_shared_state_groups_are_typed_and_compare_with_persisted_strings():
    assert "archived" in INACTIVE_TARGET_STATES
    assert "transient_failure" in PROVIDER_FAILURE_STATUSES
    assert ProviderRunStatus.AMBIGUOUS not in PROVIDER_FAILURE_STATUSES


def test_review_priority_has_one_increasing_urgency_order():
    assert [
        review_priority_rank(value)
        for value in ("none", "low", "medium", "high", "highest")
    ] == [0, 1, 2, 3, 4]
    with pytest.raises(ValueError, match="review priority must be one of"):
        review_priority_rank("urgent")


def test_domain_values_serialize_as_plain_strings():
    payload = {
        "target_state": TargetState.SYSTEM_ONLY,
        "provider_status": ProviderRunStatus.NO_MATCH,
        "assignment_role": MeasurementTargetRole.COMPOSITE_SCOPE,
        "assignment_action": MeasurementAssociationActionKind.ASSIGN,
        "priority": ReviewPriority.HIGH,
    }
    assert json.loads(json.dumps(payload)) == {
        "target_state": "system_only",
        "provider_status": "no_match",
        "assignment_role": "composite_scope",
        "assignment_action": "assign",
        "priority": "high",
    }
