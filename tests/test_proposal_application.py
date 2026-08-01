from __future__ import annotations

from sqlalchemy import select

from sdb_identity.models.photometry import MeasurementTargetAssociation
from sdb_identity.photometry.application import (
    apply_measurement_assignment_proposals,
)
from tests.test_fitting_groups import _measurement
from tests.test_system_photometry_foundation import _configured_system


def _proposal(
    measurement,
    origin,
    *,
    current_assignments,
    proposed_assignments,
):
    return {
        "measurement_id": measurement.id,
        "provider": measurement.provider,
        "source_id": measurement.source_id,
        "band": measurement.band,
        "proposal_confidence": "high",
        "proposal_reason": "test high-confidence proposal",
        "excluded": False,
        "origin_sdbid": origin.sdbid,
        "current_assignments": current_assignments,
        "proposed_assignments": proposed_assignments,
    }


def test_apply_proposals_does_not_store_ordinary_derived_default(
    session_factory, monkeypatch,
):
    _system, component_a, _component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, component_a)
    assignment = {
        "target_id": component_a.target_id,
        "sdbid": component_a.sdbid,
        "role": "contributor",
        "derived": True,
    }
    monkeypatch.setattr(
        "sdb_identity.photometry.application.measurement_assignment_proposals",
        lambda _sessions, _reference: [
            _proposal(
                measurement,
                component_a,
                current_assignments=[assignment],
                proposed_assignments=[assignment],
            )
        ],
    )

    result = apply_measurement_assignment_proposals(
        session_factory,
        target_reference=component_a.sdbid,
        apply=True,
        actor="test",
        reason="confirm ordinary attribution",
    )

    assert result["summary"]["already_current_measurements"] == 1
    with session_factory() as session:
        assert session.query(MeasurementTargetAssociation).count() == 0


def test_apply_proposals_materializes_complete_exception_over_derived_default(
    session_factory, monkeypatch,
):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    current = [{
        "target_id": system.target_id,
        "sdbid": system.sdbid,
        "role": "composite_scope",
        "derived": True,
    }]
    proposed = [
        *current,
        {
            "target_id": component_a.target_id,
            "sdbid": component_a.sdbid,
            "role": "contributor",
            "derived": False,
        },
        {
            "target_id": component_b.target_id,
            "sdbid": component_b.sdbid,
            "role": "contributor",
            "derived": False,
        },
    ]
    monkeypatch.setattr(
        "sdb_identity.photometry.application.measurement_assignment_proposals",
        lambda _sessions, _reference: [
            _proposal(
                measurement,
                system,
                current_assignments=current,
                proposed_assignments=proposed,
            )
        ],
    )

    result = apply_measurement_assignment_proposals(
        session_factory,
        target_reference=system.sdbid,
        apply=True,
        actor="test",
        reason="materialize shared-light exception",
    )

    assert result["summary"]["applied_assignments"] == 3
    assert result["items"][0]["replaces_derived_default"] is True
    with session_factory() as session:
        rows = list(session.scalars(
            select(MeasurementTargetAssociation).order_by(
                MeasurementTargetAssociation.role,
                MeasurementTargetAssociation.target_id,
            )
        ))
    assert {
        (row.target_id, row.role) for row in rows
    } == {
        (system.target_id, "composite_scope"),
        (component_a.target_id, "contributor"),
        (component_b.target_id, "contributor"),
    }
