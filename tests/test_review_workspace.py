from __future__ import annotations

from fastapi.testclient import TestClient

from sdb_identity.review_ui import create_review_app
from sdb_identity.review_workspace import (
    TargetWorkspace,
    build_target_workspace,
)
from tests.test_review_actions import _wise_measurements
from tests.test_system_photometry_foundation import _configured_system


def test_target_workspace_is_one_typed_projection(session_factory):
    system, _component_a, _component_b = _configured_system(session_factory)
    measurements = _wise_measurements(session_factory, system)

    workspace = build_target_workspace(session_factory, system.sdbid)

    assert isinstance(workspace, TargetWorkspace)
    assert workspace.sdbid == system.sdbid
    assert workspace.fitting_graph["selection"]["target"] == system.sdbid
    assert workspace.system_context["target"]["sdbid"] == system.sdbid
    assert set(workspace.raw_row_detections) == {
        measurement.raw_row_id for measurement in measurements
    }
    assert workspace.navigation is None


def test_target_workspace_api_exposes_the_rendering_projection(session_factory):
    system, _component_a, _component_b = _configured_system(session_factory)
    _wise_measurements(session_factory, system)
    client = TestClient(create_review_app(session_factory))

    response = client.get(f"/api/target/{system.sdbid}")

    assert response.status_code == 200
    value = response.json()
    assert value["sdbid"] == system.sdbid
    assert value["readiness"]["selection"]["target"] == system.sdbid
    assert value["fitting_graph"]["selection"]["target"] == system.sdbid
    assert value["system_context"]["target"]["sdbid"] == system.sdbid
    assert value["capabilities"] == {
        "catalog_update": False,
        "nearby_import": False,
    }
