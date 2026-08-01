from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from sdb_identity.adapters.allwise import AllWiseAdapter
from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.catalog_types import CatalogCandidate, MeasurementValue
from sdb_identity.catalog_decisions import CatalogDecisionService
from sdb_identity.catalog_normalization import CatalogNormalizationService
from sdb_identity.models.catalogs import (
    CatalogDetection,
    CatalogResultDecision,
    CatalogTargetAssociationAction,
    NormalizedMeasurement,
    RawCatalogRow,
)
from sdb_identity.models.identity import ExternalIdentifier
from sdb_identity.models.photometry import (
    MeasurementEligibilityAction,
    MeasurementTargetAssociation,
)
from sdb_identity.models.metadata import MetadataRun, SimbadMetadata
from sdb_identity.providers import ProviderError, SimbadNeighbour
from sdb_identity.photometry import assign_measurement_target
from sdb_identity.review_ui import create_review_app, serve_review_ui
from sdb_identity.samples import SampleService
from sdb_identity.identifiers import normalize_identifier
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.update import UpdateItem, UpdateSummary
from tests.fakes import FakeSimbad, astrometry, simbad_result
from tests.test_review_actions import _wise_measurements
from tests.test_catalog import FakeCatalog, candidate
from tests.test_system_expansion import _root_with_metadata
from tests.test_system_photometry_foundation import _configured_system


def test_catalog_overview_routes_are_structured_and_expandable(session_factory):
    client = TestClient(create_review_app(session_factory))

    response = client.get("/api/catalogs")
    assert response.status_code == 200
    assert response.json()["provider_count"] == 13
    assert response.json()["remote_count"] == 4

    page = client.get("/catalogs")
    assert page.status_code == 200
    assert "Catalog providers" in page.text
    assert "<details>" in page.text
    assert "I/259/suppl_2 is retained" in page.text


def test_review_ui_queue_preview_and_apply(session_factory, monkeypatch):
    monkeypatch.setenv("SDB_ACTOR", "browser reviewer")
    system, component_a, component_b = _configured_system(session_factory)
    with session_factory() as session:
        for index, (target, main_id) in enumerate((
            (system, "HD TEST AB"),
            (component_a, "HD TEST A"),
            (component_b, "HD TEST B"),
        ), start=1):
            run = MetadataRun(
                target_id=target.target_id,
                provider="simbad",
                release="test",
                status="match",
                is_current=True,
                query_identifier=main_id,
                candidate_count=1,
            )
            session.add(run)
            session.flush()
            session.add(SimbadMetadata(
                run_id=run.id,
                target_id=target.target_id,
                oid=index,
                main_id=main_id,
                ra_deg=10,
                dec_deg=-20,
            ))
            if index == 1:
                session.add(ExternalIdentifier(
                    target_id=target.target_id,
                    value=main_id,
                    normalized_value=normalize_identifier(main_id),
                    source="simbad_metadata",
                ))
        session.commit()
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
    assert "<th>SIMBAD name</th><th>SDB ID</th>" in queue.text
    assert "<th>providers</th>" in queue.text
    assert "<th>providers/bands</th>" not in queue.text
    assert ">HD TEST AB</a>" in queue.text
    assert f"<code>{system.sdbid}</code></a></td>" in queue.text
    assert "allwise: WISE3P4" not in queue.text

    workspace = client.get(f"/target/{system.sdbid}")
    assert workspace.status_code == 200
    assert "WISE3P4" in workspace.text
    assert "WISE22" in workspace.text
    assert "class='composite-scope' checked" in workspace.text
    assert "Measurement applies to the combined system" in workspace.text
    assert "System target" in workspace.text
    assert f'src="/target/{system.sdbid}/sky"' in workspace.text
    assert "SDB_RAW_ROW_DETECTIONS" in workspace.text
    assert str(measurements[0].raw_row_id) in workspace.text
    assert "sdb-review-selection" in workspace.text
    assert "Decide target role" not in workspace.text
    assert "Change target role" in workspace.text
    assert workspace.text.count("class='external-resource'") == 7
    assert workspace.text.index("class='external-resource'") < workspace.text.index(
        'id="catalog-coverage"'
    )
    assert (
        "https://simbad.cds.unistra.fr/simbad/sim-id?"
        "submit=submit+id&amp;Ident=HD+TEST+AB"
        in workspace.text
    )
    assert "https://cdsportal.u-strasbg.fr/?target=10.0+-20.0" in workspace.text
    assert "cassis.sirtf.com/atlas/cgi/radec.py?ra=10.0" in workspace.text
    assert "locstr=10.0%2C-20.0" in workspace.text
    assert "WorldPt=10.0%3B-20.0%3BEQ_J2000" in workspace.text
    assert "searchQuery=10.0%2C-20.0" in workspace.text
    assert "hips=AllWISE+color" in workspace.text
    assert "Physical / fitted model" in workspace.text
    assert "Composite / measurement scope" in workspace.text
    assert ".drawer-open .live-workspace iframe" in workspace.text
    assert "document.body.classList.toggle('drawer-open',reviewDrawerVisible)" in workspace.text
    assert "drawer.hidden=!reviewDrawerVisible" in workspace.text
    assert "drawer.hidden=false" not in workspace.text
    assert "sdb-review-drawer-toggle" in workspace.text
    assert "sdb-review-drawer-state" in workspace.text
    assert "sessionStorage.getItem('sdb-review-tools-visible')" in workspace.text
    assert 'id="relatives-dialog"' in workspace.text
    assert "renderHumanSummary" in workspace.text
    assert "/api/relatives/preview" in workspace.text
    assert "Preview include/exclude" in workspace.text
    assert "Fit include/exclude preview" in workspace.text
    assert "Included in fit" in workspace.text
    assert "Exclude from fit" in workspace.text
    assert "Leave unchanged" not in workspace.text
    assert "included · included" not in workspace.text
    assert 'class="preview-grid"' in workspace.text
    assert 'class="drawer-actions"' in workspace.text
    assert "--drawer-width:min(420px,38vw)" in workspace.text
    assert "/api/eligibility/preview" in workspace.text
    assert "/api/catalog-association/preview" in workspace.text
    assert "Accept for this target" in workspace.text
    assert "Reject for this target" in workspace.text
    assert 'id="actor" value="browser reviewer"' in workspace.text
    assert "prefillReason('reason',currentPreview)" in workspace.text
    assert 'placeholder="Preview suggests a reason"' in workspace.text
    assert "<code>HD TEST A</code> (physical)" in workspace.text
    assert "<code>HD TEST B</code> (physical)" in workspace.text
    assert 'id="catalog-coverage-dialog"' in workspace.text
    assert "/api/catalog-coverage/preview" in workspace.text
    assert 'id="nearby-import-dialog"' in workspace.text
    assert "/api/nearby-import/search" in workspace.text
    assert "/api/nearby-import/apply" in workspace.text

    sky = client.get(f"/target/{system.sdbid}/sky?radius=42")
    assert sky.status_code == 200
    assert '<body class="embedded">' in sky.text
    assert '"radius_arcsec": 42.0' in sky.text
    assert 'id="system-context-radius-form"' in sky.text
    assert 'id="system-context-radius"' in sky.text
    assert 'url.searchParams.set("radius", String(radius))' in sky.text
    assert "window.parent.postMessage" in sky.text
    assert "Plotly.newPlot" in sky.text
    assert "sdb-review-relatives" in sky.text
    assert 'id="toggle-review-drawer"' in sky.text
    assert "Show review tools" in sky.text
    assert "sdb-review-drawer-ready" in sky.text
    assert "sdb-review-drawer-toggle" in sky.text
    assert "sdb-review-drawer-state" in sky.text

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
    assert preview_value["suggested_reason"].startswith("Reviewed allwise source")
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
            "measurement_id": next(
                row.id for row in measurements if row.band == "WISE22"
            ),
            "excluded": True,
        }],
    })
    assert eligibility_preview.status_code == 200
    eligibility_value = eligibility_preview.json()
    assert eligibility_value["human_summary"]["title"] == (
        "Fit include/exclude changes ready"
    )
    eligibility_apply = client.post("/api/eligibility/apply", json={
        "changes": [{
            "measurement_id": next(
                row.id for row in measurements if row.band == "WISE22"
            ),
            "excluded": True,
        }],
        "actor": "browser reviewer",
        "reason": "poor catalog quality",
        "state_token": eligibility_value["state_token"],
    })
    assert eligibility_apply.status_code == 200
    assert eligibility_apply.json()["applied"]["actions_added"] == 1
    with session_factory() as session:
        action = session.scalar(select(MeasurementEligibilityAction))
    assert (action.measurement_id, action.excluded) == (
        next(row.id for row in measurements if row.band == "WISE22"),
        True,
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


def test_review_ui_applies_catalog_target_association_without_provider_query(
    session_factory, monkeypatch,
):
    monkeypatch.setenv("SDB_ACTOR", "browser reviewer")
    identity = IdentityService(session_factory)
    parent = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    component = identity.add(
        AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
    )
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("parent", ra=10.0, dec=0.0),
            candidate(
                "component",
                ra=10.0 + 4.1 / 3600.0,
                dec=0.0,
            ),
        ]),
    }).refresh(parent.sdbid, "2mass")
    with session_factory() as session:
        detection, raw = session.execute(
            select(CatalogDetection, RawCatalogRow)
            .join(
                RawCatalogRow,
                RawCatalogRow.detection_id == CatalogDetection.id,
            )
            .where(CatalogDetection.source_id == "component")
        ).one()

    client = TestClient(create_review_app(session_factory))
    payload = {
        "target": component.sdbid,
        "detection_id": detection.id,
        "raw_row_id": raw.id,
        "action": "accept",
    }
    preview = client.post(
        "/api/catalog-association/preview", json=payload,
    )
    assert preview.status_code == 200
    value = preview.json()
    assert value["has_changes"] is True
    assert value["human_summary"]["title"] == (
        "Catalog source association ready"
    )
    applied = client.post(
        "/api/catalog-association/apply",
        json={
            **payload,
            "state_token": value["state_token"],
            "actor": "browser reviewer",
            "reason": value["suggested_reason"],
        },
    )
    assert applied.status_code == 200
    with session_factory() as session:
        action = session.scalar(select(CatalogTargetAssociationAction))
        assert action is not None
        assert action.target_id == component.target_id
        assert action.detection_id == detection.id
        assert action.action == "accept"


def test_review_ui_places_source_association_before_photometry_controls(
    session_factory, monkeypatch,
):
    monkeypatch.setenv("SDB_ACTOR", "browser reviewer")
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10.0, dec_deg=0.0)
    )
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("source", ra=10.0, dec=0.0),
        ]),
    }).refresh(target.sdbid, "2mass")

    workspace = TestClient(
        create_review_app(session_factory)
    ).get(f"/target/{target.sdbid}")

    assert workspace.status_code == 200
    assert workspace.text.index(
        'id="catalog-association-editor"'
    ) < workspace.text.index('id="detection-editors"')


def test_review_ui_assignment_drawer_uses_snapshot_catalog_display_id(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    CatalogAcquisitionService(session_factory, {
        "hip2": FakeCatalog(
            [CatalogCandidate(
                source_id="36948",
                ra_deg=10,
                dec_deg=-20,
                epoch=1991.25,
                payload={"HIP": 36948},
                measurements=(MeasurementValue(
                    band="HP",
                    value=8.36,
                    resolution_major_arcsec=0.1,
                    resolution_minor_arcsec=0.1,
                    resolution_kind="test",
                    resolution_reference="test",
                ),),
            )],
            name="hip2",
            release="fake-hip2",
            query_epoch=1991.25,
        ),
    }).refresh(target.sdbid, "hip2")

    client = TestClient(create_review_app(session_factory))
    workspace = client.get(f"/target/{target.sdbid}")
    sky = client.get(f"/target/{target.sdbid}/sky")

    assert workspace.status_code == 200
    assert "<h3>hip2 · HIP 36948</h3>" in workspace.text
    assert "pointDisplayId(point)" in workspace.text
    assert sky.status_code == 200
    assert '"source_display_name": "HIP 36948"' in sky.text


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
        "reconcile": 0,
        "complete": 0,
        "context_only": 2,
        "review_required": 1,
    }
    assert value["human_summary"]["title"] == "SIMBAD-relative changes ready"
    assert any("Import HD 1B" in row for row in value["human_summary"]["changes"])
    assert any(
        "HD 1 b — planet" in row
        for row in value["human_summary"]["warnings"]
    )

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

    current = client.post(
        "/api/relatives/preview", json={"target": root.sdbid},
    ).json()
    assert current["has_changes"] is False
    assert current["counts"]["complete"] == 1
    assert current["counts"]["import"] == 0
    assert current["counts"]["reconcile"] == 0

    _wise_measurements(session_factory, root)
    workspace = client.get(f"/target/{root.sdbid}")
    assert workspace.status_code == 200
    assert "<code>HD 1 AB</code>" in workspace.text
    assert "<code>HD 1B</code> (physical)" in workspace.text


def test_review_ui_searches_and_imports_selected_nearby_simbad_objects(
    session_factory,
):
    simbad = FakeSimbad(
        resolutions={
            "HD 1": simbad_result(
                "HD   1",
                astrometry(10.0, -20.0, source="simbad"),
            ),
            "HD   1B": simbad_result(
                "HD   1B",
                astrometry(10.001, -20.0, source="simbad"),
            ),
        },
        neighbours=[
            SimbadNeighbour(
                1,
                "HD   1",
                astrometry(10.0, -20.0, source="simbad"),
                0.0,
                primary_object_type="Star",
                object_type_label="Star",
            ),
            SimbadNeighbour(
                2,
                "HD   1B",
                astrometry(10.001, -20.0, source="simbad"),
                3.38,
                primary_object_type="Star",
                object_type_label="Star",
                spectral_type="M3V",
            ),
            SimbadNeighbour(
                3,
                "HD   1b",
                astrometry(10.0, -20.0, source="simbad"),
                0.0,
                primary_object_type="Planet",
                object_type_label="Planet",
                object_types=("Planet",),
            ),
        ],
    )
    identity = IdentityService(session_factory, simbad=simbad)
    root = identity.add(AddRequest(name="HD 1"))
    update_calls = []

    class FakeUpdateService:
        def update_targets(self, targets, *, providers, force):
            update_calls.append((tuple(targets), tuple(providers), force))
            return UpdateSummary(
                target_count=len(tuple(targets)),
                refreshed=0,
                skipped=0,
                missing=0,
                failed=0,
                items=(),
            )

    client = TestClient(create_review_app(
        session_factory,
        identity_service_factory=lambda: identity,
        catalog_coverage_providers=("gaia_dr3", "2mass"),
        catalog_update_factory=FakeUpdateService,
    ))

    workspace = client.get(f"/target/{root.sdbid}")
    assert workspace.status_code == 200
    assert '<button id="nearby-import" type="button"' in workspace.text
    assert "Import nearby SIMBAD objects" in workspace.text

    searched = client.post("/api/nearby-import/search", json={
        "target": root.sdbid,
        "radius_arcsec": 60,
    })
    assert searched.status_code == 200
    search = searched.json()
    assert search["new_count"] == 1
    assert search["blocked_count"] == 1
    assert search["candidates"][0]["current_target"] is True
    assert search["candidates"][0]["selectable"] is False
    assert search["candidates"][1]["main_id"] == "HD   1b"
    assert search["candidates"][1]["blocked_reason"] == "planet"
    assert search["candidates"][1]["selectable"] is False
    assert search["candidates"][2]["main_id"] == "HD   1B"
    assert search["candidates"][2]["selectable"] is True

    applied = client.post("/api/nearby-import/apply", json={
        "target": root.sdbid,
        "main_ids": ["HD   1B"],
    })
    assert applied.status_code == 200
    value = applied.json()
    assert value["created_count"] == 1
    assert value["failed_count"] == 0
    assert value["items"][0]["requested_name"] == "HD   1B"
    assert value["human_summary"]["title"].startswith(
        "Nearby import finished"
    )
    assert update_calls == [(
        (value["items"][0]["sdbid"],),
        ("simbad", "gaia_dr3", "2mass"),
        False,
    )]


def test_review_ui_previews_catalog_accept_no_match_and_retry(session_factory):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    adapter = AllWiseAdapter()
    rows = [
        {
            "AllWISE": "one", "RAJ2000": 10.00010, "DEJ2000": -20,
            "qph": "AAAA", "ccf": "0000", "W1mag": 7.0, "e_W1mag": 0.1,
        },
        {
            "AllWISE": "two", "RAJ2000": 10.00011, "DEJ2000": -20,
            "qph": "AAAA", "ccf": "0000", "W1mag": 8.0, "e_W1mag": 0.1,
        },
    ]
    adapter.query = lambda context: [
        adapter.parse_row(row) for row in rows
    ]
    service = CatalogAcquisitionService(session_factory, {"allwise": adapter})
    decisions = CatalogDecisionService(
        session_factory, {"allwise": adapter}, acquisition=service,
    )
    ambiguous = service.refresh(target.sdbid, "allwise")
    with session_factory() as session:
        candidate = session.scalar(
            select(RawCatalogRow)
            .where(RawCatalogRow.run_id == ambiguous.run_id)
            .order_by(RawCatalogRow.id.desc())
        )

    client = TestClient(create_review_app(
        session_factory,
        catalog_service_factory=lambda provider, action: decisions,
    ))
    workspace = client.get(f"/target/{target.sdbid}")
    assert "Preview accept candidate" in workspace.text
    assert "Preview no match" in workspace.text
    assert "/api/provider-result/preview" in workspace.text

    accepted = client.post("/api/provider-result/preview", json={
        "action": "accept_candidate",
        "run_id": ambiguous.run_id,
        "raw_row_id": candidate.id,
    })
    assert accepted.status_code == 200
    assert accepted.json()["human_summary"]["title"] == (
        "Catalog candidate ready to accept"
    )

    no_match = client.post("/api/provider-result/preview", json={
        "action": "reviewed_no_match",
        "run_id": ambiguous.run_id,
        "raw_row_id": candidate.id,
    })
    assert no_match.status_code == 200
    no_match_value = no_match.json()
    applied = client.post("/api/provider-result/apply", json={
        "action": "reviewed_no_match",
        "run_id": ambiguous.run_id,
        "raw_row_id": candidate.id,
        "actor": "browser reviewer",
        "reason": "neither candidate is the target",
        "state_token": no_match_value["state_token"],
    })
    assert applied.status_code == 200
    assert applied.json()["applied"]["status"] == "no_match"

    stale = client.post("/api/provider-result/apply", json={
        "action": "accept_candidate",
        "run_id": ambiguous.run_id,
        "raw_row_id": candidate.id,
        "actor": "browser reviewer",
        "reason": "stale choice",
        "state_token": accepted.json()["state_token"],
    })
    assert stale.status_code == 409
    assert "changed after preview" in stale.json()["detail"]

    repeated_preview = client.post("/api/provider-result/preview", json={
        "action": "reviewed_no_match",
        "run_id": ambiguous.run_id,
        "raw_row_id": candidate.id,
    })
    assert repeated_preview.status_code == 200
    assert repeated_preview.json()["has_changes"] is False
    with session_factory() as session:
        action = session.scalar(
            select(CatalogResultDecision)
            .where(CatalogResultDecision.action == "reviewed_no_match")
        )
        assert action.reason == "neither candidate is the target"
        assert session.query(CatalogResultDecision).count() == 1

    def fail(_context):
        raise ProviderError("temporary provider failure", transient=True)

    adapter.query = fail
    failed = service.refresh(target.sdbid, "allwise")
    sky = client.get(f"/target/{target.sdbid}/sky")
    assert "provider failure" in sky.text
    assert "transient_failure" in sky.text
    retry = client.post("/api/provider-result/preview", json={
        "action": "retry",
        "run_id": failed.run_id,
        "raw_row_id": None,
    })
    assert retry.status_code == 200
    assert retry.json()["human_summary"]["title"] == "Provider retry ready"


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


def test_review_ui_collapses_ordinary_attribution_as_an_exception(
    session_factory,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    _wise_measurements(session_factory, target, source_id="ordinary-wise")

    workspace = TestClient(create_review_app(session_factory)).get(
        f"/target/{target.sdbid}"
    )

    assert workspace.status_code == 200
    assert "Photometry follows the accepted source association" in workspace.text
    assert "No separate assignment decision is needed" in workspace.text
    assert "Change attribution (exception)" in workspace.text


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
    assert "Measurement applies to the combined system" not in target.text

    filtered_out = client.get(
        f"/target/{ordered[0]}",
        params={"search": ordered[1], "position": 0},
    )
    assert filtered_out.status_code == 200
    assert "resolved/filtered out · 1 remain" in filtered_out.text
    assert quote(ordered[1]) in filtered_out.text


def test_review_ui_catalog_coverage_previews_and_updates_only_system_gaps(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    calls = []

    class FakeUpdateService:
        def update_targets(self, target_references, *, providers, force):
            calls.append((tuple(target_references), tuple(providers), force))
            return UpdateSummary(
                target_count=len(tuple(target_references)),
                refreshed=1,
                skipped=0,
                missing=0,
                failed=0,
                items=(
                    UpdateItem(
                        system.target_id,
                        system.sdbid,
                        "2mass",
                        "refreshed",
                        "no_match",
                    ),
                ),
            )

    client = TestClient(create_review_app(
        session_factory,
        catalog_coverage_providers=("2mass", "tycho2"),
        catalog_update_factory=FakeUpdateService,
    ))
    workspace = client.get(f"/target/{component_b.sdbid}")
    assert workspace.status_code == 200
    assert "Catalog coverage 0/6" in workspace.text

    preview_response = client.post(
        "/api/catalog-coverage/preview",
        json={"target": component_b.sdbid},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["missing_count"] == 6
    assert preview["update_available"] is True
    assert {
        row["target_sdbid"] for row in preview["coverage"]
    } == {system.sdbid, component_a.sdbid, component_b.sdbid}

    applied = client.post(
        "/api/catalog-coverage/apply",
        json={
            "target": component_b.sdbid,
            "state_token": preview["state_token"],
        },
    )
    assert applied.status_code == 200
    assert applied.json()["applied"]["refreshed"] == 1
    assert calls == [(
        (system.sdbid, component_a.sdbid, component_b.sdbid),
        ("2mass", "tycho2"),
        False,
    )]


def test_review_ui_catalog_coverage_normalizes_stored_candidates_offline(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    adapter = AllWiseAdapter()
    query_count = 0

    def query(context):
        nonlocal query_count
        query_count += 1
        return [
            adapter.parse_row({
                "AllWISE": f"J004000.0{index}-200000.0",
                "RAJ2000": 10.00010 + index * 0.00001,
                "DEJ2000": -20.0,
                "W1mag": 7.0 + index,
                "e_W1mag": 0.02,
                "qph": "AAAA",
                "ccf": "0000",
            })
            for index in range(2)
        ]

    adapter.query = query
    service = CatalogAcquisitionService(session_factory, {"allwise": adapter})
    assert service.refresh(target.sdbid, "allwise").status == "ambiguous"
    with session_factory.begin() as session:
        session.execute(delete(NormalizedMeasurement))
        for detection in session.scalars(select(CatalogDetection)):
            detection.normalization_status = "pending"
            detection.normalized_at = None

    client = TestClient(create_review_app(
        session_factory,
        catalog_coverage_providers=("allwise",),
        catalog_service_factory=lambda provider, action: CatalogNormalizationService(
            session_factory, {"allwise": adapter},
        ),
    ))
    preview = client.post(
        "/api/catalog-coverage/preview",
        json={"target": target.sdbid},
    ).json()

    assert preview["missing_count"] == 0
    assert preview["normalization_count"] == 2
    assert preview["update_available"] is False
    assert preview["action_available"] is True

    applied = client.post(
        "/api/catalog-coverage/apply",
        json={"target": target.sdbid, "state_token": preview["state_token"]},
    )

    assert applied.status_code == 200
    assert applied.json()["normalization_applied"][0]["completed"] == 2
    assert query_count == 1
    with session_factory() as session:
        assert session.query(NormalizedMeasurement).count() == 2


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
