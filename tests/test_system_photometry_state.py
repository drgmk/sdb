from __future__ import annotations

from sdb_identity.photometry.state import load_system_photometry_state
from tests.test_fitting_groups import _assign_pair, _measurement
from tests.test_system_photometry_foundation import _configured_system


def test_expanded_state_collects_one_consistent_system_fact_set(session_factory):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    _assign_pair(
        session_factory, measurement, system, component_a, component_b,
    )

    with session_factory() as session:
        state = load_system_photometry_state(
            session, [component_a.target_id],
        )

    assert state.selected_target_ids == frozenset({component_a.target_id})
    assert state.context_target_ids == frozenset({
        system.target_id,
        component_a.target_id,
        component_b.target_id,
    })
    assert set(state.targets) == set(state.context_target_ids)
    assert state.lifecycle[system.target_id].role.value == "composite"
    assert state.lifecycle[component_a.target_id].role.value == "physical"
    assert state.system_memberships[component_b.target_id][0].component_label == "B"

    assert set(state.measurements) == {measurement.id}
    assert set(state.detections) == {measurement.detection_id}
    assert state.encounter_target_ids == {
        measurement.id: frozenset({system.target_id}),
    }
    assert state.detection_target_ids == {
        measurement.detection_id: frozenset({system.target_id}),
    }
    assert {row.target_id for row in state.assignments} == {
        system.target_id,
        component_a.target_id,
        component_b.target_id,
    }
    assert state.eligibility[measurement.id].excluded is False
    assert measurement.raw_row_id in state.raw_rows
    assert state.invariant_errors() == ()


def test_selected_only_state_does_not_absorb_system_relative_photometry(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    _measurement(session_factory, system)

    with session_factory() as session:
        state = load_system_photometry_state(
            session, [component_a.target_id], expand_context=False,
        )

    assert state.context_target_ids == frozenset({component_a.target_id})
    assert component_b.target_id not in state.targets
    assert state.measurements == {}
    assert state.encounters == ()
    assert state.invariant_errors() == ()
