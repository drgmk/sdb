from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import select

from sdb_identity.models import MeasurementTargetAssociation, PhotometryOverride
from sdb_identity.photometry import assign_measurement_target
from sdb_identity.review_ui import create_review_app, serve_review_ui
from sdb_identity.samples import SampleService
from sdb_identity.service import AddRequest, IdentityService
from tests.fakes import FakeSimbad, astrometry, simbad_result
from tests.test_review_actions import _wise_measurements
from tests.test_system_expansion import _root_with_metadata
from tests.test_system_photometry_foundation import _configured_system


def test_review_ui_queue_preview_and_apply(session_factory):
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
            reason="current composite scope",
        )
    samples = SampleService(session_factory)
    samples.create("review-ui")
    samples.add(
        "review-ui", system.sdbid, actor="test", reason="UI fixture",
    )

    client = TestClient(create_review_app(session_factory, sample="review-ui"))
    queue = client.get("/")
    assert queue.status_code == 200
    assert system.sdbid in queue.text

    workspace = client.get(f"/target/{system.sdbid}")
    assert workspace.status_code == 200
    assert "WISE3P4" in workspace.text
    assert "WISE22" in workspace.text
    assert "class=\"composite-scope\" checked" in workspace.text
    assert f'src="/target/{system.sdbid}/sky"' in workspace.text
    assert "SDB_RAW_ROW_DETECTIONS" in workspace.text
    assert str(measurements[0].raw_row_id) in workspace.text
    assert "sdb-review-selection" in workspace.text
    assert "Decide target role" not in workspace.text
    assert "Change target role" in workspace.text
    assert "Physical / fitted model" in workspace.text
    assert "Composite / measurement scope" in workspace.text
    assert ".drawer-open .live-workspace iframe" in workspace.text
    assert "document.body.classList.add('drawer-open')" in workspace.text
    assert "document.body.classList.remove('drawer-open')" in workspace.text
    assert 'id="relatives-dialog"' in workspace.text
    assert "renderHumanSummary" in workspace.text
    assert "/api/relatives/preview" in workspace.text
    assert "Preview fit eligibility" in workspace.text
    assert "Include in fit/export" in workspace.text
    assert "Show but exclude from fit" in workspace.text
    assert "/api/eligibility/preview" in workspace.text

    sky = client.get(f"/target/{system.sdbid}/sky")
    assert sky.status_code == 200
    assert '<body class="embedded">' in sky.text
    assert "window.parent.postMessage" in sky.text
    assert "Plotly.newPlot" in sky.text
    assert "sdb-review-relatives" in sky.text

    payload = {
        "detection_id": measurements[0].detection_id,
        "scope_target": system.sdbid,
        "contributors": [component_a.sdbid, component_b.sdbid],
        "include_composite_scope": True,
        "measurement_ids": [row.id for row in measurements],
        "target_role": "",
        "target_state": "",
    }
    preview = client.post("/api/decision/preview", json=payload)
    assert preview.status_code == 200
    preview_value = preview.json()
    assert preview_value["has_changes"] is True
    assert len(preview_value["add_assignments"]) == 4
    assert preview_value["human_summary"]["title"] == "Decision changes ready"
    assert any("Add contributor" in row for row in preview_value["human_summary"]["changes"])

    applied = client.post("/api/decision/apply", json={
        **payload,
        "actor": "browser reviewer",
        "reason": "WISE covers both components",
        "state_token": preview_value["state_token"],
    })
    assert applied.status_code == 200
    assert applied.json()["applied"]["assignments_added"] == 4
    with session_factory() as session:
        associations = list(session.scalars(select(MeasurementTargetAssociation)))
    assert len(associations) == 6

    eligibility_preview = client.post("/api/eligibility/preview", json={
        "changes": [{
            "target": system.sdbid,
            "provider": "allwise",
            "band": "WISE22",
            "excluded": True,
        }],
    })
    assert eligibility_preview.status_code == 200
    eligibility_value = eligibility_preview.json()
    assert eligibility_value["human_summary"]["title"] == (
        "Fit-eligibility changes ready"
    )
    eligibility_apply = client.post("/api/eligibility/apply", json={
        "changes": [{
            "target": system.sdbid,
            "provider": "allwise",
            "band": "WISE22",
            "excluded": True,
        }],
        "actor": "browser reviewer",
        "reason": "poor catalog quality",
        "state_token": eligibility_value["state_token"],
    })
    assert eligibility_apply.status_code == 200
    assert eligibility_apply.json()["applied"]["overrides_added"] == 1
    with session_factory() as session:
        override = session.scalar(select(PhotometryOverride))
    assert (override.target_id, override.provider, override.band, override.excluded) == (
        system.target_id, "allwise", "WISE22", True,
    )

    lifecycle_preview = client.post("/api/lifecycle/preview", json={
        "target": system.sdbid,
        "role": "physical",
        "state": "active",
    })
    assert lifecycle_preview.status_code == 200
    lifecycle_value = lifecycle_preview.json()
    assert lifecycle_value["interpretation"]["model_target"] is True
    assert lifecycle_value["human_summary"]["title"] == "Role changes ready"
    lifecycle_apply = client.post("/api/lifecycle/apply", json={
        "target": system.sdbid,
        "role": "physical",
        "state": "active",
        "actor": "browser reviewer",
        "reason": "use one combined-light model",
        "state_token": lifecycle_value["state_token"],
    })
    assert lifecycle_apply.status_code == 200
    assert lifecycle_apply.json()["applied"]["lifecycle_actions"] == 1


def test_review_ui_previews_and_imports_immediate_simbad_relatives(session_factory):
    root = _root_with_metadata(session_factory)
    identity = IdentityService(
        session_factory,
        simbad=FakeSimbad({
            "HD 1B": simbad_result(
                "HD 1B",
                astrometry(10.001, -20.0, source="simbad"),
                ("WDS J00400-2000B",),
            ),
        }),
    )
    client = TestClient(create_review_app(
        session_factory,
        identity_service_factory=lambda: identity,
    ))

    preview = client.post("/api/relatives/preview", json={"target": root.sdbid})
    assert preview.status_code == 200
    value = preview.json()
    assert value["has_changes"] is True
    assert value["counts"] == {
        "import": 1,
        "already_imported": 0,
        "context_only": 2,
        "review_required": 1,
    }
    assert value["human_summary"]["title"] == "SIMBAD-relative changes ready"
    assert any("Import HD 1B" in row for row in value["human_summary"]["changes"])

    applied = client.post("/api/relatives/apply", json={
        "target": root.sdbid,
        "actor": "browser reviewer",
        "reason": "import immediate stellar relatives",
        "state_token": value["state_token"],
    })
    assert applied.status_code == 200
    result = applied.json()
    assert result["imported"] == 1
    assert result["failed"] == 0
    assert result["human_summary"]["title"].startswith("Relative import finished")
    assert any("Imported HD 1B" in row for row in result["human_summary"]["changes"])


def test_review_ui_relative_import_is_disabled_without_live_identity_service(session_factory):
    root = _root_with_metadata(session_factory)
    client = TestClient(create_review_app(session_factory))
    preview = client.post("/api/relatives/preview", json={"target": root.sdbid})

    applied = client.post("/api/relatives/apply", json={
        "target": root.sdbid,
        "actor": "browser reviewer",
        "reason": "should fail offline",
        "state_token": preview.json()["state_token"],
    })

    assert applied.status_code == 409
    assert "offline review mode" in applied.json()["detail"]


def test_review_ui_filters_and_navigates_the_unresolved_queue(session_factory):
    identity = IdentityService(session_factory)
    first = identity.add(AddRequest(ra_deg=10, dec_deg=-20))
    second = identity.add(AddRequest(ra_deg=10.0002, dec_deg=-20))
    samples = SampleService(session_factory)
    samples.create("navigation")
    for index, target in enumerate((first, second)):
        measurement = _wise_measurements(
            session_factory,
            target,
            source_id=f"navigation-wise-{index}",
            ra=10 + index * 0.0002,
        )[0]
        assign_measurement_target(
            session_factory,
            measurement.id,
            target.sdbid,
            role="composite_scope",
            method="fixture",
            actor="test",
            reason="unresolved queue fixture",
        )
        samples.add(
            "navigation", target.sdbid, actor="test", reason="queue fixture",
        )

    ordered = sorted([first.sdbid, second.sdbid])
    client = TestClient(create_review_app(session_factory, sample="navigation"))
    queue = client.get("/")
    assert queue.status_code == 200
    assert "Showing <strong>2</strong> of 2 sample targets" in queue.text
    assert 'name="priority"' in queue.text
    assert 'name="classification"' in queue.text
    assert 'name="provider"' in queue.text
    assert f"position=0" in queue.text

    filtered = client.get("/", params={"search": second.sdbid})
    assert filtered.status_code == 200
    assert "Showing <strong>1</strong> of 2 sample targets" in filtered.text
    assert second.sdbid in filtered.text
    assert first.sdbid not in filtered.text

    target = client.get(
        f"/target/{ordered[0]}", params={"position": 0},
    )
    assert target.status_code == 200
    assert "1 of 2" in target.text
    assert "rel='next'" in target.text
    assert quote(ordered[1]) in target.text

    filtered_out = client.get(
        f"/target/{ordered[0]}",
        params={"search": ordered[1], "position": 0},
    )
    assert filtered_out.status_code == 200
    assert "resolved/filtered out · 1 remain" in filtered_out.text
    assert quote(ordered[1]) in filtered_out.text


def test_review_ui_refuses_non_local_bind(session_factory):
    try:
        serve_review_ui(
            session_factory,
            sample=None,
            host="0.0.0.0",
            port=8765,
        )
    except ValueError as error:
        assert "localhost only" in str(error)
    else:  # pragma: no cover - explicit failure is more informative than pytest helper
        raise AssertionError("non-local review bind was accepted")
