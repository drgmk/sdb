from __future__ import annotations

from sqlalchemy import select

from sdb_identity.models import MeasurementEligibilityAction
from sdb_identity.photometry import assign_measurement_target
from sdb_identity.review_commands import (
    review_detection_command,
    review_eligibility_command,
    review_lifecycle_command,
)
from tests.test_review_actions import _wise_measurements
from tests.test_system_photometry_foundation import _configured_system


def test_review_commands_preview_and_apply_without_http(session_factory):
    system, component_a, component_b = _configured_system(session_factory)
    measurements = _wise_measurements(session_factory, system)
    for measurement in measurements:
        assign_measurement_target(
            session_factory,
            measurement.id,
            system.sdbid,
            role="composite_scope",
            method="fixture",
            actor="test",
            reason="current combined-light scope",
        )

    decision = review_detection_command(
        session_factory,
        {
            "detection_id": measurements[0].detection_id,
            "scope_target": system.sdbid,
            "contributors": [component_a.sdbid, component_b.sdbid],
            "include_composite_scope": True,
            "measurement_ids": [row.id for row in measurements],
            "target_role": "",
            "target_state": "",
        },
        apply=False,
    )
    assert decision["mode"] == "preview"
    assert decision["human_summary"]["title"] == "Decision changes ready"

    lifecycle = review_lifecycle_command(
        session_factory,
        {
            "target": system.sdbid,
            "role": "physical",
            "state": "active",
        },
        apply=False,
    )
    assert lifecycle["mode"] == "preview"
    assert lifecycle["human_summary"]["title"] == "Role changes ready"

    measurement = next(row for row in measurements if row.band == "WISE22")
    eligibility_payload = {
        "changes": [{"measurement_id": measurement.id, "excluded": True}],
    }
    eligibility = review_eligibility_command(
        session_factory,
        eligibility_payload,
        apply=False,
    )
    assert eligibility["human_summary"]["title"] == (
        "Fit include/exclude changes ready"
    )
    applied = review_eligibility_command(
        session_factory,
        {
            **eligibility_payload,
            "actor": "command reviewer",
            "reason": eligibility["suggested_reason"],
            "state_token": eligibility["state_token"],
        },
        apply=True,
    )
    assert applied["human_summary"]["title"] == (
        "Applied 1 fit include/exclude change"
    )
    with session_factory() as session:
        action = session.scalar(select(MeasurementEligibilityAction))
    assert action is not None
    assert action.measurement_id == measurement.id
    assert action.actor == "command reviewer"
