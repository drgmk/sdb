from __future__ import annotations

import gzip
import json
import math
import math

import pytest
from astropy.table import Table
from sqlalchemy import func, inspect, select

from sdb_identity.database import make_engine
from sdb_identity.dirty import pending_export_targets
from sdb_identity.hierarchy import HierarchyService
from sdb_identity.metadata import MetadataQueryResult, MetadataService, RelationshipValue
from sdb_identity.models import (
    AstrometricSolution,
    CatalogDetection,
    CatalogRun,
    ExternalIdentifier,
    HierarchyMatchAction,
    HierarchyMatchCandidate,
    HierarchyRecord,
    HierarchySource,
    MatchCandidate,
    NormalizedMeasurement,
    RawCatalogRow,
    StructuralEdge,
    StructuralEdgeAction,
    Submission,
    TargetSystem,
    utcnow,
)
from sdb_identity.identifiers import normalize_identifier
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.samples import SampleService
from tests.test_metadata import FakeMetadataProvider, snapshot


def _catalog_detection(session, run, source_id, ra_deg, dec_deg, epoch, payload_json):
    detection = CatalogDetection(
        provider=run.provider,
        release=run.release,
        detection_key=source_id,
        source_id=source_id,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        epoch=epoch,
        payload_json=payload_json,
    )
    session.add(detection)
    session.flush()
    return detection


def test_hierarchy_schema_created(db_path):
    inspector = inspect(make_engine(db_path))

    for table in (
        "hierarchy_sources",
        "hierarchy_records",
        "structural_edges",
        "structural_edge_actions",
        "hierarchy_match_candidates",
        "hierarchy_match_actions",
        "target_systems",
        "target_system_members",
        "measurement_target_associations",
    ):
        assert table in inspector.get_table_names()
    assert "hierarchy_relationship_summary" in inspector.get_view_names()
    assert "hierarchy_system_members" in inspector.get_view_names()
    assert "hierarchy_match_review" in inspector.get_view_names()
    assert "hierarchy_match_action_history" in inspector.get_view_names()
    assert "hierarchy_graph_effective" in inspector.get_view_names()


def test_hierarchy_service_creates_system_members_and_relationships(session_factory):
    identity = IdentityService(session_factory)
    primary = identity.add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    secondary = identity.add(AddRequest(ra_deg=10.001, dec_deg=-20.0))

    service = HierarchyService(session_factory)
    system = service.create_system("test binary", primary=primary.sdbid, note="manual seed")
    member = service.add_member(system.name, secondary.sdbid, component_label="B")
    relationship = service.add_relationship(
        relationship_type="pair",
        system=system.name,
        primary=primary.sdbid,
        secondary=secondary.sdbid,
        component="AB",
        source="manual",
        separation_arcsec=3.4,
        pa_deg=91.0,
        relation_epoch=2026.5,
        actor="tester",
        reason="unit test",
    )

    status = service.status(primary.sdbid).as_dict()

    assert system.id is not None
    assert member.component_label == "B"
    assert relationship.id is not None
    assert status["systems"][0]["name"] == "test binary"
    assert len(status["systems"][0]["members"]) == 2
    assert status["relationships"][0]["component"] == "AB"
    assert status["relationships"][0]["secondary_sdbid"] == secondary.sdbid
    assert {target.sdbid for target, _count, _created in pending_export_targets(session_factory)} == {
        primary.sdbid,
        secondary.sdbid,
    }


def test_hierarchy_target_context_reports_photometry_blending(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\t\t1.425\t45.8166667\t2024\t90\t0.2\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=3)
    service.derive_graph("wds", source_id=imported.source_id)
    with session_factory.begin() as session:
        run = CatalogRun(
            target_id=target.target_id,
            provider="allwise",
            release="test",
            status="match",
            is_current=True,
            query_ra_deg=1.425,
            query_dec_deg=45.8166667,
            query_epoch=2010.5,
            candidate_count=1,
            selected_source_id="WISEA test",
            completed_at=utcnow(),
        )
        session.add(run)
        session.flush()
        payload_json = json.dumps({
            "nb": 2,
            "na": 0,
            "prox": 1.7,
            "_r": 0.1,
        }, sort_keys=True)
        detection = _catalog_detection(
            session, run, "WISEA test", 1.425, 45.8166667, 2010.5,
            payload_json,
        )
        raw = RawCatalogRow(
            run_id=run.id,
            detection_id=detection.id,
            source_id="WISEA test",
            ra_deg=1.425,
            dec_deg=45.8166667,
            epoch=2010.5,
            separation_arcsec=0.0,
            score=1.0,
            accepted=True,
            payload_json=payload_json,
        )
        session.add(raw)
        session.flush()
        session.add(NormalizedMeasurement(
            run_id=run.id,
            target_id=target.target_id,
            raw_row_id=raw.id,
            detection_id=detection.id,
            measurement_key="WISE3P4:0",
            provider="allwise",
            source_id="WISEA test",
            band="WISE3P4",
            value=7.1,
            unit="mag",
            bibcode="",
            resolution_major_arcsec=6.1,
            resolution_minor_arcsec=6.1,
            resolution_kind="psf_fwhm",
            resolution_reference="AllWISE Explanatory Supplement",
        ))

    context = service.target_context(target.sdbid)

    photometry = context["photometry_context"]
    assert photometry["nearest_pair_arcsec"] == pytest.approx(0.2)
    assert photometry["likely_blended_bands"] == ["allwise:WISE3P4"]
    assert photometry["predicted_scope_counts"] == {"shared": 1}
    assert photometry["predicted_blend_counts"] == {"blended": 1}
    assert photometry["bands"][0]["predicted_blend_state"] == "blended"
    assert photometry["bands"][0]["predicted_ownership_scope"] == "shared"
    assert photometry["bands"][0]["predicted_blend_reason"] == "unresolved_at_catalog_resolution"
    system_context = service.system_context(target.sdbid)
    neighbourhood = system_context["catalog_neighbourhood_by_target"][target.sdbid][0]
    assert neighbourhood["neighbourhood_flags"] == {
        "active_deblend": 0,
        "simultaneous_psf_components": 2,
    }


def test_hierarchy_system_context_links_sibling_identity_candidate_and_photometry(
    session_factory,
    tmp_path,
):
    identity = IdentityService(session_factory)
    primary = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    secondary = identity.add(AddRequest(ra_deg=10.0019444444, dec_deg=0.0))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00400+0000\tTEST\tAB\t10.0\t0.0\t2024\t90\t7.0\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=10)
    service.derive_graph("wds", source_id=imported.source_id)
    with session_factory.begin() as session:
        session.add(AstrometricSolution(
            target_id=secondary.target_id,
            source="gaia_dr3",
            source_id="123456789",
            ra_deg=10.0019444444,
            dec_deg=0.0,
            epoch=2016.0,
            proper_motion_available=False,
            derived_ra2000_deg=10.0019444444,
            derived_dec2000_deg=0.0,
        ))
        session.add(ExternalIdentifier(
            target_id=secondary.target_id,
            value="Gaia DR3 123456789",
            normalized_value=normalize_identifier("Gaia DR3 123456789"),
            source="simbad",
        ))
        submission = Submission(target_id=primary.target_id, status="created")
        session.add(submission)
        session.flush()
        session.add(MatchCandidate(
            submission_id=submission.id,
            provider="gaia_dr3",
            source_id="123456789",
            ra_deg=10.0019444444,
            dec_deg=0.0,
            epoch=2016.0,
            proper_motion_available=False,
            separation_arcsec=7.0,
            score=0.01,
            score_details="{}",
            accepted=False,
        ))
        for target_id, source_id in (
            (primary.target_id, "WISEA"),
            (secondary.target_id, "WISEB"),
        ):
            run = CatalogRun(
                target_id=target_id,
                provider="allwise",
                release="test",
                status="match",
                is_current=True,
                query_ra_deg=10.0,
                query_dec_deg=0.0,
                query_epoch=2010.5,
                candidate_count=1,
                selected_source_id=source_id,
                completed_at=utcnow(),
            )
            session.add(run)
            session.flush()
            detection = _catalog_detection(
                session, run, source_id, 10.0, 0.0, 2010.5, "{}",
            )
            raw = RawCatalogRow(
                run_id=run.id,
                detection_id=detection.id,
                source_id=source_id,
                ra_deg=10.0,
                dec_deg=0.0,
                epoch=2010.5,
                separation_arcsec=0.0,
                score=1.0,
                accepted=True,
                payload_json="{}",
            )
            session.add(raw)
            session.flush()
            session.add(NormalizedMeasurement(
                run_id=run.id,
                target_id=target_id,
                raw_row_id=raw.id,
                detection_id=detection.id,
                measurement_key="WISE22:0",
                provider="allwise",
                source_id=source_id,
                band="WISE22",
                value=7.1,
                unit="mag",
                bibcode="",
                resolution_major_arcsec=12.0,
                resolution_minor_arcsec=12.0,
                resolution_kind="psf_fwhm",
                resolution_reference="test",
            ))

    context = service.system_context(primary.sdbid)

    assert [row["sdbid"] for row in context["nearby_sdb_targets"][:2]] == [
        primary.sdbid,
        secondary.sdbid,
    ]
    assert {
        row["component"] for row in context["component_positions"]
    } >= {"A", "B"}
    components = {
        row["component"]: row
        for row in context["component_positions"]
    }
    assert components["A"]["linked_sdbid"] == primary.sdbid
    assert components["A"]["component_target_role"] == "current_target"
    assert components["A"]["component_match_basis"] == "position"
    assert components["B"]["linked_sdbid"] == secondary.sdbid
    assert components["B"]["component_target_role"] == "sibling_target"
    assert components["B"]["component_match_basis"] == "position"
    assert components["B"]["component_match_separation_arcsec"] == pytest.approx(0.0, abs=1e-3)
    assert context["identity_cross_candidates"][0]["source_id"] == "123456789"
    assert context["identity_cross_candidates"][0]["matched_nearby_targets"][0]["sdbid"] == secondary.sdbid
    assert set(context["photometry_by_target"]) == {primary.sdbid, secondary.sdbid}


def test_hierarchy_system_context_is_order_independent_for_component_imported_first(
    session_factory,
    tmp_path,
):
    identity = IdentityService(session_factory)
    secondary = identity.add(AddRequest(ra_deg=10.0019444444, dec_deg=0.0))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=secondary.target_id,
            value="WDS J00400+0000B",
            normalized_value=normalize_identifier("WDS J00400+0000B"),
            source="simbad",
        ))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00400+0000\tTEST\tAB\t10.0\t0.0\t2024\t90\t7.0\n"
        "00400+0000\tTEST\tAC\t10.0\t0.0\t2024\t180\t45.0\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=10)
    service.derive_graph("wds", source_id=imported.source_id)

    context = service.system_context(secondary.sdbid)

    assert [row["sdbid"] for row in context["nearby_sdb_targets"]] == [secondary.sdbid]
    components = {
        row["component"]: row
        for row in context["component_positions"]
    }
    assert set(components) >= {"A", "B", "C"}
    assert components["B"]["linked_sdbid"] == secondary.sdbid
    assert components["B"]["component_target_role"] == "current_target"
    assert components["B"]["component_match_basis"] == "identifier+position"
    assert components["B"]["component_match_conflict"] is None
    assert components["A"]["linked_sdbid"] is None
    assert components["A"]["component_target_role"] == "known_unimported_component"
    assert components["C"]["linked_sdbid"] is None
    assert components["C"]["component_target_role"] == "known_unimported_component"
    assert context["target_context"]["component_assignment"]["nearest_component"] == "B"


def test_hierarchy_photometry_context_prefers_accepted_candidate(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\t\t1.425\t45.8166667\t2024\t90\t0.2\n"
        "00058+4549\tSTF3051\t\t1.425\t45.8166667\t2024\t90\t10.0\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=3)
    service.derive_graph("wds", source_id=imported.source_id)
    with session_factory() as session:
        far_candidate = session.scalar(
            select(HierarchyMatchCandidate)
            .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
            .where(HierarchyRecord.native_id == "00058+4549")
        )
    service.accept_match(far_candidate.id, actor="tester", reason="wide pair is the reviewed match")
    with session_factory.begin() as session:
        run = CatalogRun(
            target_id=target.target_id,
            provider="allwise",
            release="test",
            status="match",
            is_current=True,
            query_ra_deg=1.425,
            query_dec_deg=45.8166667,
            query_epoch=2010.5,
            candidate_count=1,
            selected_source_id="WISEA test",
            completed_at=utcnow(),
        )
        session.add(run)
        session.flush()
        detection = _catalog_detection(
            session, run, "WISEA test", 1.425, 45.8166667, 2010.5, "{}",
        )
        raw = RawCatalogRow(
            run_id=run.id,
            detection_id=detection.id,
            source_id="WISEA test",
            ra_deg=1.425,
            dec_deg=45.8166667,
            epoch=2010.5,
            separation_arcsec=0.0,
            score=1.0,
            accepted=True,
            payload_json="{}",
        )
        session.add(raw)
        session.flush()
        session.add(NormalizedMeasurement(
            run_id=run.id,
            target_id=target.target_id,
            raw_row_id=raw.id,
            detection_id=detection.id,
            measurement_key="WISE3P4:0",
            provider="allwise",
            source_id="WISEA test",
            band="WISE3P4",
            value=7.1,
            unit="mag",
            bibcode="",
            resolution_major_arcsec=6.1,
            resolution_minor_arcsec=6.1,
        ))

    context = service.target_context(target.sdbid)

    assert context["hierarchy_decision_basis"] == "accepted_candidates"
    photometry = context["photometry_context"]
    assert photometry["nearest_pair_arcsec"] == pytest.approx(10.0)
    assert photometry["likely_blended_bands"] == []
    assert photometry["predicted_scope_counts"] == {"component": 1}
    assert photometry["predicted_blend_counts"] == {"clear": 1}
    assert photometry["bands"][0]["predicted_blend_state"] == "clear"
    assert photometry["bands"][0]["predicted_blend_reason"] == "resolved_at_catalog_resolution"
    assert photometry["bands"][0]["predicted_ownership_scope"] == "component"
    assert photometry["bands"][0]["predicted_blend_state"] == "clear"


def test_system_target_scope_respects_catalog_resolution():
    from sdb_identity.hierarchy import _photometry_scope_prediction

    common = {
        "target_level": "system",
        "assignment_status": "semantic_group_contains_nearest_component",
        "semantic_kind": "system_or_parent",
        "stored_ownership_scope": "component",
        "stored_blend_state": "clear",
    }
    resolved = _photometry_scope_prediction(
        **common,
        blend_prediction="likely_resolved_at_catalog_resolution",
    )
    blended = _photometry_scope_prediction(
        **common,
        blend_prediction="likely_blended_at_catalog_resolution",
    )

    assert resolved["predicted_ownership_scope"] == "component"
    assert resolved["predicted_blend_state"] == "clear"
    assert blended["predicted_ownership_scope"] == "system"
    assert blended["predicted_blend_state"] == "blended"
    assert blended["predicted_blend_reason"] == "unresolved_at_catalog_resolution"


def test_hierarchy_photometry_review_filters_sample_provider_and_blends(
    session_factory,
    tmp_path,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    other = IdentityService(session_factory).add(AddRequest(ra_deg=20, dec_deg=30))
    samples = SampleService(session_factory)
    samples.create("science")
    samples.add("science", target.sdbid, actor="tester", reason="unit test")
    samples.add("science", other.sdbid, actor="tester", reason="unit test")
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\t\t1.425\t45.8166667\t2024\t90\t0.2\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=3)
    service.derive_graph("wds", source_id=imported.source_id)
    with session_factory.begin() as session:
        run = CatalogRun(
            target_id=target.target_id,
            provider="allwise",
            release="test",
            status="match",
            is_current=True,
            query_ra_deg=1.425,
            query_dec_deg=45.8166667,
            query_epoch=2010.5,
            candidate_count=1,
            selected_source_id="WISEA test",
            completed_at=utcnow(),
        )
        session.add(run)
        session.flush()
        detection = _catalog_detection(
            session, run, "WISEA test", 1.425, 45.8166667, 2010.5, "{}",
        )
        raw = RawCatalogRow(
            run_id=run.id,
            detection_id=detection.id,
            source_id="WISEA test",
            ra_deg=1.425,
            dec_deg=45.8166667,
            epoch=2010.5,
            separation_arcsec=0.0,
            score=1.0,
            accepted=True,
            payload_json="{}",
        )
        session.add(raw)
        session.flush()
        session.add(NormalizedMeasurement(
            run_id=run.id,
            target_id=target.target_id,
            raw_row_id=raw.id,
            detection_id=detection.id,
            measurement_key="WISE3P4:0",
            provider="allwise",
            source_id="WISEA test",
            band="WISE3P4",
            value=7.1,
            unit="mag",
            bibcode="",
            resolution_major_arcsec=6.1,
            resolution_minor_arcsec=6.1,
        ))

    rows = service.photometry_review(
        [member.sdbid for member in samples.members("science")],
        provider="allwise",
        blended_only=True,
    )

    assert [row["sdbid"] for row in rows] == [target.sdbid]
    assert rows[0]["likely_blended_bands"] == ["allwise:WISE3P4"]
    assert rows[0]["measurement_count"] == 1
    assert rows[0]["predicted_scope_counts"] == {"shared": 1}
    assert rows[0]["bands"][0]["scope_reason"] == (
        "catalog resolution is larger than the nearest known component separation"
    )


def test_hierarchy_review_queue_prioritizes_blended_candidate_review(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\t\t1.425\t45.8166667\t2024\t90\t0.2\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=3)
    service.derive_graph("wds", source_id=imported.source_id)
    with session_factory.begin() as session:
        run = CatalogRun(
            target_id=target.target_id,
            provider="allwise",
            release="test",
            status="match",
            is_current=True,
            query_ra_deg=1.425,
            query_dec_deg=45.8166667,
            query_epoch=2010.5,
            candidate_count=1,
            selected_source_id="WISEA test",
            completed_at=utcnow(),
        )
        session.add(run)
        session.flush()
        detection = _catalog_detection(
            session, run, "WISEA test", 1.425, 45.8166667, 2010.5, "{}",
        )
        raw = RawCatalogRow(
            run_id=run.id,
            detection_id=detection.id,
            source_id="WISEA test",
            ra_deg=1.425,
            dec_deg=45.8166667,
            epoch=2010.5,
            separation_arcsec=0.0,
            score=1.0,
            accepted=True,
            payload_json="{}",
        )
        session.add(raw)
        session.flush()
        session.add(NormalizedMeasurement(
            run_id=run.id,
            target_id=target.target_id,
            raw_row_id=raw.id,
            detection_id=detection.id,
            measurement_key="WISE3P4:0",
            provider="allwise",
            source_id="WISEA test",
            band="WISE3P4",
            value=7.1,
            unit="mag",
            bibcode="",
            resolution_major_arcsec=6.1,
            resolution_minor_arcsec=6.1,
        ))

    rows = service.review_queue([target.sdbid])

    assert rows[0]["sdbid"] == target.sdbid
    assert rows[0]["priority"] == "highest"
    assert rows[0]["basis"] == "candidate_review"
    assert rows[0]["candidate_count"] == 1
    assert rows[0]["likely_blended_bands"] == ["allwise:WISE3P4"]
    assert "unaccepted hierarchy candidates" in rows[0]["reason"]
    assert target.sdbid in rows[0]["review_view_hint"]


def test_hierarchy_cli_creates_and_reports_system(tmp_path, capsys):
    from sdb_identity.cli import main

    database = tmp_path / "hierarchy.sqlite"
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    assert main(["--database", str(database), "--offline", "add", "--ra", "10", "--dec", "-20"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["--database", str(database), "--offline", "add", "--ra", "10.001", "--dec", "-20"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert main([
        "--database", str(database), "hierarchy", "create-system",
        "cli binary", "--primary", first["sdbid"],
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "cli binary"

    assert main([
        "--database", str(database), "hierarchy", "add-member",
        "cli binary", second["sdbid"], "--component", "B",
    ]) == 0
    capsys.readouterr()

    assert main([
        "--database", str(database), "hierarchy", "add-relationship",
        "--type", "pair", "--system", "cli binary",
        "--primary", first["sdbid"], "--secondary", second["sdbid"],
        "--component", "AB", "--separation", "3.4", "--pa", "91",
        "--actor", "tester", "--reason", "cli test",
    ]) == 0
    relationship = json.loads(capsys.readouterr().out)
    assert relationship["relationship_type"] == "pair"

    assert main(["--database", str(database), "hierarchy", "status", first["sdbid"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["systems"][0]["name"] == "cli binary"
    assert status["relationships"][0]["component"] == "AB"


def test_hierarchy_relationship_requires_a_target(session_factory):
    service = HierarchyService(session_factory)

    try:
        service.add_relationship(relationship_type="pair")
    except ValueError as error:
        assert "at least one target" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_hierarchy_imports_wds_fixed_snapshot(session_factory, tmp_path):
    path = tmp_path / "wds.txt"
    path.write_text(
        "00057+4549STF3050 AB  1822 2024   99 120 134    1.2      2.345  7.10  8.20 G0\n"
        "not a data row\n",
        encoding="utf-8",
    )

    result = HierarchyService(session_factory).import_snapshot(
        "wds", path, release="test-2026.5",
    )

    with session_factory() as session:
        source = session.get(HierarchySource, result.source_id)
        record = session.scalar(select(HierarchyRecord).where(HierarchyRecord.source_id == result.source_id))

    assert result.row_count == 1
    assert result.skipped_count == 1
    assert source.provider == "wds"
    assert record.native_id == "00057+4549"
    assert record.component == "AB"
    assert record.discoverer_id == "STF3050"
    assert record.last_epoch == 2024
    assert record.measure_epoch == 2024
    assert record.pa_deg == 134
    assert record.separation_arcsec == 2.345
    assert record.delta_mag == 1.1
    assert abs(record.ra_deg - 1.425) < 1e-6
    assert abs(record.dec_deg - 45.8166667) < 1e-6


def test_hierarchy_import_reuses_existing_source_with_same_checksum(session_factory, tmp_path):
    path = tmp_path / "wds.txt"
    path.write_text(
        "00057+4549STF3050 AB  1822 2024   99 120 134    1.2      2.345  7.10  8.20 G0\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)

    first = service.import_snapshot("wds", path, release="test-2026.5")
    second = service.import_snapshot("wds", path, release="test-2026.5")

    assert second.source_id == first.source_id
    assert second.row_count == first.row_count
    with session_factory() as session:
        assert session.query(HierarchySource).count() == 1
        assert session.query(HierarchyRecord).count() == 1


def test_hierarchy_prunes_duplicate_sources_and_dependent_rows(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    checksum = "duplicate-checksum"
    with session_factory.begin() as session:
        keep = HierarchySource(provider="wds", release="test", checksum=checksum)
        duplicate = HierarchySource(provider="wds", release="test", checksum=checksum)
        session.add_all([keep, duplicate])
        session.flush()
        keep_record = HierarchyRecord(
            source_id=keep.id,
            provider="wds",
            native_id="00057+4549",
            component="AB",
            raw_payload_json="{}",
        )
        duplicate_record = HierarchyRecord(
            source_id=duplicate.id,
            provider="wds",
            native_id="00057+4549",
            component="AB",
            raw_payload_json="{}",
        )
        session.add_all([keep_record, duplicate_record])
        session.flush()
        duplicate_candidate = HierarchyMatchCandidate(
            record_id=duplicate_record.id,
            target_id=target.target_id,
            provider="wds",
            match_method="position",
            score=1.0,
            separation_arcsec=0.1,
            reason="test duplicate",
        )
        session.add(duplicate_candidate)
        duplicate_edge = StructuralEdge(
            source_id=duplicate.id,
            record_id=duplicate_record.id,
            source="wds",
            native_id="00057+4549",
            relation_type="component",
            structural_role="structural",
            status="derived",
            geometry_status="usable",
        )
        session.add(duplicate_edge)
        session.flush()
        session.add(HierarchyMatchAction(
            candidate_id=duplicate_candidate.id,
            action="reject",
            previous_status="candidate",
            new_status="rejected",
            actor="test",
            reason="test duplicate action",
        ))
        session.add(StructuralEdgeAction(
            edge_id=duplicate_edge.id,
            source="wds",
            native_id="00057+4549",
            action="deactivate",
            actor="test",
            reason="test duplicate override",
        ))

    result = HierarchyService(session_factory).prune_duplicate_sources("wds")

    assert result.groups == 1
    assert result.removed_sources == 1
    assert result.removed_records == 1
    assert result.removed_candidates == 1
    assert result.removed_match_actions == 1
    assert result.removed_graph_edges == 1
    assert result.removed_graph_overrides == 1
    with session_factory() as session:
        assert session.query(HierarchySource).count() == 1
        assert session.query(HierarchyRecord).count() == 1
        assert session.scalar(select(HierarchyRecord).where(HierarchyRecord.source_id == keep.id)) is not None


def test_hierarchy_skips_wds_fixed_rows_with_dubious_x_note(session_factory, tmp_path):
    path = tmp_path / "wds.txt"
    good = "00057+4549STF3050 AB  1822 2024   99 120 134    1.2      2.345  7.10  8.20 G0"
    dubious = "00058+4549STF3051 AB  1822 2024   99 120 134    1.2      2.345  7.10  8.20 G0"
    path.write_text(
        good + "\n" + dubious.ljust(107) + "X\n",
        encoding="utf-8",
    )

    result = HierarchyService(session_factory).import_snapshot(
        "wds", path, release="test-2026.5",
    )

    with session_factory() as session:
        records = list(session.scalars(select(HierarchyRecord).where(
            HierarchyRecord.source_id == result.source_id
        )))

    assert result.row_count == 1
    assert result.skipped_count == 1
    assert records[0].native_id == "00057+4549"


def test_hierarchy_imports_wds_delimited_snapshot_using_recent_measure(session_factory, tmp_path):
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs1,Obs2,PA1,PA2,Sep1,Sep2,Mag1,Mag2\n"
        "00057+4549,STF3050,AB,1.425,45.8167,1822,2024,120,134,1.2,2.345,7.1,8.2\n",
        encoding="utf-8",
    )

    result = HierarchyService(session_factory).import_snapshot(
        "wds", path, release="test-release",
    )

    with session_factory() as session:
        record = session.scalar(select(HierarchyRecord).where(
            HierarchyRecord.source_id == result.source_id
        ))

    assert record.first_epoch == 1822
    assert record.last_epoch == 2024
    assert record.measure_epoch == 2024
    assert record.pa_deg == 134
    assert record.separation_arcsec == 2.345
    assert record.ra_deg == 1.425
    assert record.dec_deg == 45.8167


def test_hierarchy_fetch_reuses_vizier_snapshot_cache(session_factory, tmp_path):
    class FakeClient:
        def __init__(self):
            self.table_calls = 0
            self.readme_calls = 0

        def fetch_tables(self, catalog):
            self.table_calls += 1
            main_table = Table(
                rows=[("00057+4549", "STF3050", "AB", 1.425, 45.8167, 2024, 134, 2.345)],
                names=("WDS", "Discov", "Comp", "RAJ2000", "DEJ2000", "Obs2", "PA2", "Sep2"),
            )
            main_table.meta["name"] = f"{catalog}/wds"
            notes_table = Table(
                rows=[("00057+4549", "STF3050", "note text", "Ref2026")],
                names=("WDS", "Disc", "Text", "RefCode"),
            )
            notes_table.meta["name"] = f"{catalog}/notes"
            return [main_table, notes_table]

        def fetch_readme(self, catalog):
            self.readme_calls += 1
            return "WDS ReadMe updated 2026-07-13"

        def source_url(self, catalog):
            return f"https://example.invalid/{catalog}"

    client = FakeClient()
    service = HierarchyService(session_factory)
    cache_path = tmp_path / "sdb-cache.sqlite"

    first = service.fetch_snapshot("wds", client=client, cache_path=cache_path)
    second = service.fetch_snapshot("wds", client=client, cache_path=cache_path)

    with session_factory() as session:
        sources = tuple(session.scalars(select(HierarchySource).order_by(HierarchySource.id)))
        records = tuple(session.scalars(select(HierarchyRecord).order_by(HierarchyRecord.id)))

    assert client.table_calls == 1
    assert client.readme_calls == 1
    assert first.row_count == 1
    assert second.row_count == 1
    assert first.checksum == second.checksum
    assert second.source_id == first.source_id
    assert len(sources) == 1
    assert "cache_status=stored" in sources[0].note
    assert [record.separation_arcsec for record in records] == [2.345]


def test_hierarchy_fetch_parses_only_configured_main_table(session_factory, tmp_path):
    class FakeClient:
        def fetch_tables(self, catalog):
            main_table = Table(
                rows=[("J01553-6019AB", "AB", 28.875, -60.3167)],
                names=("CCDM", "Comp", "RAJ2000", "DEJ2000"),
            )
            main_table.meta["name"] = "I/274/ccdm"
            ref_table = Table(
                rows=[("J01553-6019AB", "Ref2026", "reference text")],
                names=("CCDM", "RefCode", "Text"),
            )
            ref_table.meta["name"] = "I/274/refs"
            return [main_table, ref_table]

        def fetch_readme(self, catalog):
            return "CCDM ReadMe updated 2026-07-13"

        def source_url(self, catalog):
            return f"https://example.invalid/{catalog}"

    service = HierarchyService(session_factory)
    result = service.fetch_snapshot("ccdm", client=FakeClient(), cache_path=tmp_path / "cache.sqlite")

    with session_factory() as session:
        records = tuple(session.scalars(select(HierarchyRecord)))

    assert result.row_count == 1
    assert len(records) == 1
    assert records[0].native_id == "J01553-6019AB"


def test_hierarchy_skips_wds_delimited_rows_with_dubious_x_note(session_factory, tmp_path):
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Sep2,Notes\n"
        "00057+4549,STF3050,AB,1.425,45.8167,2.345,\n"
        "00058+4549,STF3051,AB,1.430,45.8167,2.345,X\n",
        encoding="utf-8",
    )

    result = HierarchyService(session_factory).import_snapshot(
        "wds", path, release="test-release",
    )

    with session_factory() as session:
        records = list(session.scalars(select(HierarchyRecord).where(
            HierarchyRecord.source_id == result.source_id
        )))

    assert result.row_count == 1
    assert result.skipped_count == 1
    assert records[0].native_id == "00057+4549"


def test_hierarchy_imports_ccdm_delimited_snapshot(session_factory, tmp_path):
    path = tmp_path / "ccdm.tsv"
    path.write_text(
        "CCDM\tComp\tRAJ2000\tDEJ2000\tMag1\tMag2\n"
        "J01553-6019AB\tAB\t28.875\t-60.3167\t6.1\t7.4\n",
        encoding="utf-8",
    )

    result = HierarchyService(session_factory).import_snapshot(
        "ccdm", path, release="test-ccdm",
    )

    with session_factory() as session:
        record = session.scalar(select(HierarchyRecord).where(HierarchyRecord.source_id == result.source_id))

    assert result.row_count == 1
    assert record.provider == "ccdm"
    assert record.native_id == "J01553-6019AB"
    assert record.component == "AB"
    assert record.ra_deg == 28.875
    assert record.dec_deg == -60.3167
    assert record.delta_mag == 1.3


def test_hierarchy_imports_gzipped_snapshot(session_factory, tmp_path):
    path = tmp_path / "ccdm.dat.gz"
    rows = (
        " 00000+3852 A          +001.11-0026.2 10                02  6.6 B9*"
        "-0005-0001+38 5108.0+38 2462.6 224699  A17157  23549N3819A      3\n"
        " 00000+3852 B  BU  860                   1881 107   6.7  7 11.4    "
        "-0005-0001                             A17157  23549N3819B      3\n"
    )
    path.write_bytes(gzip.compress(rows.encode()))

    result = HierarchyService(session_factory).import_snapshot(
        "ccdm", path, release="test-ccdm-gz",
    )

    with session_factory() as session:
        records = tuple(session.scalars(
            select(HierarchyRecord)
            .where(HierarchyRecord.source_id == result.source_id)
            .order_by(HierarchyRecord.id)
        ))

    assert result.row_count == 2
    assert records[0].native_id == "00000+3852"
    assert records[0].component == "A"
    assert records[0].dec_deg == pytest.approx(38.8593888889)
    assert records[0].magnitude_primary == 6.6
    assert records[1].component == "B"
    assert records[1].discoverer_id == "BU 860"
    assert records[1].measure_epoch == 1881
    assert records[1].pa_deg == 107
    assert records[1].separation_arcsec == 6.7


def test_hierarchy_imports_ccdm_precise_j2000_remainders(session_factory, tmp_path):
    path = tmp_path / "ccdm.dat.gz"
    rows = (
        " 04153-0739 A          -001.71-0010.4 10                05  4.5 G5*"
        "-2245-3419-07  780.0  131063.8  26965  A 3093  04108S0749A  19849\n"
    )
    path.write_bytes(gzip.compress(rows.encode()))

    result = HierarchyService(session_factory).import_snapshot(
        "ccdm", path, release="test-ccdm-gz",
    )

    with session_factory() as session:
        record = session.scalar(select(HierarchyRecord).where(HierarchyRecord.source_id == result.source_id))

    assert record.component == "A"
    assert record.ra_deg == pytest.approx(63.817875)
    assert record.dec_deg == pytest.approx(-7.6528888889)
    payload = json.loads(record.raw_payload_json)
    assert payload["dRAs"] == -1.71
    assert payload["dDEs"] == -10.4
    assert payload["pmRA_masyr"] == -2245
    assert payload["pmDE_masyr"] == -3419


def test_hierarchy_position_match_uses_component_endpoint(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    endpoint_ra = base_ra + 3.0 / (3600.0 * math.cos(math.radians(base_dec)))
    target = IdentityService(session_factory).add(AddRequest(ra_deg=endpoint_ra, dec_deg=base_dec))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        f"00057+4549,STF3050,AB,{base_ra},{base_dec},2024,90,3.0,7.1,8.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    assert candidate is not None
    assert candidate.target_id == target.target_id
    assert candidate.match_method == "position"
    assert candidate.separation_arcsec < 0.01
    assert "component endpoint separation" in candidate.reason


def test_hierarchy_wds_999_separation_is_not_used_as_component_endpoint(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    endpoint_ra = base_ra + 999.9 / (3600.0 * math.cos(math.radians(base_dec)))
    endpoint = IdentityService(session_factory).add(AddRequest(ra_deg=endpoint_ra, dec_deg=base_dec))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        f"00057+4549,STF3050,AC,{base_ra},{base_dec},2024,90,999.9,7.1,8.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))
        record = session.scalar(select(HierarchyRecord))
        payload = json.loads(record.raw_payload_json)

    assert candidate is None
    assert endpoint.target_id is not None
    assert record.separation_arcsec is None
    assert record.pa_deg is None
    assert payload["unusable_separation_arcsec"] == pytest.approx(999.9)
    assert payload["unusable_separation_reason"] == "WDS 999.9 separation sentinel"


def test_hierarchy_derives_wds_graph_edges(session_factory, tmp_path):
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "05287-6527,AAA,AB,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        '05287-6527,AAA,"Aa,Ab",82.187,-65.449,2024,45,0.2,7.1,7.2\n'
        '05287-6527,AAA,"Ba,Bb",82.190,-65.449,2024,270,0.3,8.1,8.2\n'
        '05287-6527,AAA,"AB,C",82.187,-65.449,2024,180,20.0,7.1,10.2\n'
        "05287-6527,AAA,AC,82.187,-65.449,2024,180,999.9,7.1,10.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    result = service.derive_graph("wds", source_id=imported.source_id)
    rows = service.graph_edges(provider="wds", native_id="05287-6527", source_id=imported.source_id)

    assert result.record_count == 5
    assert result.edge_count == 4
    assert result.skipped_count == 1
    assert sorted((row.reference_label, row.component_label, row.relation_type) for row in rows) == [
        ("A", "B", "group"),
        ("AB", "C", "group"),
        ("Aa", "Ab", "internal"),
        ("Ba", "Bb", "internal"),
    ]
    assert {
        (row.reference_label, row.component_label): row.structural_role
        for row in rows
    } == {
        ("A", "B"): "structural",
        ("AB", "C"): "structural",
        ("Aa", "Ab"): "structural",
        ("Ba", "Bb"): "structural",
    }
    group_edge = next(row for row in rows if row.reference_label == "AB" and row.component_label == "C")
    ab_edge = next(row for row in rows if row.reference_label == "A" and row.component_label == "B")
    assert group_edge.start_ra_deg != pytest.approx(ab_edge.start_ra_deg)
    assert group_edge.geometry_status == "usable"
    with session_factory() as session:
        assert session.query(StructuralEdge).count() == 4


def test_hierarchy_graph_overrides_are_append_only_and_effective(session_factory, tmp_path):
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "05287-6527,AAA,BC,82.187,-65.449,2024,180,20.0,7.1,10.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.derive_graph("wds", source_id=imported.source_id)
    before = service.graph_edges(provider="wds", native_id="05287-6527", source_id=imported.source_id)[0]
    result = service.override_graph_edge(
        provider="wds",
        native_id="05287-6527",
        reference_label="B",
        component_label="C",
        source_id=imported.source_id,
        status="rejected",
        relation_type="cross_link",
        structural_role="structural",
        actor="tester",
        reason="triangular relationship is display-only",
    )
    after = service.graph_edges(provider="wds", native_id="05287-6527", source_id=imported.source_id)[0]

    assert before.status == "derived"
    assert result.previous_status == "derived"
    assert after.status == "rejected"
    assert after.relation_type == "cross_link"
    assert after.structural_role == "structural"
    assert after.override_id == result.override_id
    service.derive_graph("wds", source_id=imported.source_id)
    rebuilt = service.graph_edges(provider="wds", native_id="05287-6527", source_id=imported.source_id)[0]
    assert rebuilt.status == "rejected"
    assert rebuilt.structural_role == "structural"
    assert rebuilt.override_id == result.override_id
    with session_factory() as session:
        assert session.query(StructuralEdgeAction).count() == 1


def test_hierarchy_wds_non_a_pairs_are_structural_when_unambiguous(session_factory, tmp_path):
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "00369+7034,AAA,DE,82.187,-65.449,2024,90,5.0,7.1,8.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.derive_graph("wds", source_id=imported.source_id)
    edge = service.graph_edges(provider="wds", native_id="00369+7034", source_id=imported.source_id)[0]

    assert edge.reference_label == "D"
    assert edge.component_label == "E"
    assert edge.relation_type == "group"
    assert edge.structural_role == "structural"


def test_hierarchy_target_context_reports_nearest_component(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    endpoint_ra = base_ra + 3.0 / (3600.0 * math.cos(math.radians(base_dec)))
    target = IdentityService(session_factory).add(AddRequest(ra_deg=endpoint_ra, dec_deg=base_dec))
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (snapshot("HD 1 AB"),))),
    ).refresh(target.sdbid)
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        f"00057+4549,STF3050,AB,{base_ra},{base_dec},2024,90,3.0,7.1,8.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)
    service.derive_graph("wds", source_id=imported.source_id)

    context = service.target_context(target.sdbid)
    summary = service.target_context_summary(target.sdbid)

    assert context["classification"] == "component_of_known_system"
    assert context["matched_systems"] == 1
    assert context["nearest_component"]["component"] == "B"
    assert context["nearest_component"]["separation_arcsec"] < 0.01
    assert context["closest_companion"]["component"] == "A"
    assert context["semantic_identity"]["kind"] == "system_or_parent"
    assert context["semantic_identity"]["main_id"] == "HD 1 AB"
    assert [item["main_id"] for item in context["semantic_identity"]["parents"]] == ["Cluster 1"]
    assert [item["main_id"] for item in context["semantic_identity"]["children"]] == ["HD 1 B"]
    assert context["semantic_identity"]["parents"][0]["component_relevance"] == "contextual_group"
    assert context["semantic_identity"]["children"][0]["component_relevance"] == "stellar_or_substellar_component"
    assert context["semantic_identity"]["component_label_candidates"][0]["label"] == "AB"
    assert context["component_assignment"]["status"] == "semantic_group_contains_nearest_component"
    assert context["component_assignment"]["nearest_component"] == "B"
    assert context["component_assignment"]["semantic_component"] == "AB"
    assert summary["nearest_component"]["component"] == "B"
    assert summary["nearby_components"] == 2
    assert summary["semantic_identity"] == {
        "children": 1,
        "confidence": "high",
        "evidence": "simbad_relationships",
        "kind": "system_or_parent",
        "main_id": "HD 1 AB",
        "component_label_candidates": [{
            "confidence": "medium",
            "label": "AB",
            "source": "main_id",
            "value": "HD 1 AB",
        }],
        "parents": 1,
        "relationship_relevance_counts": {
            "contextual_group": 1,
            "planetary_or_disk": 0,
            "stellar_or_substellar_component": 1,
            "unknown": 0,
        },
        "status": "match",
    }
    assert summary["component_assignment"]["status"] == "semantic_group_contains_nearest_component"
    assert summary["component_assignment"]["nearest_component"] == "B"


def test_hierarchy_target_context_classifies_simbad_relationship_shapes(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    service = HierarchyService(session_factory)

    assert service.target_context(target.sdbid)["semantic_identity"]["kind"] == "unknown"

    parent_only = snapshot("HD 1 B", spectral_type="K0V")
    parent_only = parent_only.__class__(
        **{
            **parent_only.__dict__,
            "relationships": tuple(
                relationship for relationship in parent_only.relationships
                if relationship.direction == "parent"
            ),
        }
    )
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (parent_only,))),
    ).refresh(target.sdbid)
    semantic = service.target_context(target.sdbid)["semantic_identity"]
    assert semantic["kind"] == "single_or_no_known_hierarchy"
    assert len(semantic["parents"]) == 1
    assert semantic["children"] == []

    children_only = snapshot("HD 1", spectral_type="F5V")
    children_only = children_only.__class__(
        **{
            **children_only.__dict__,
            "relationships": tuple(
                relationship for relationship in children_only.relationships
                if relationship.direction == "child"
            ),
        }
    )
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (children_only,))),
    ).refresh(target.sdbid)
    semantic = service.target_context(target.sdbid)["semantic_identity"]
    assert semantic["kind"] == "system_or_parent"
    assert semantic["parents"] == []
    assert len(semantic["children"]) == 1


def test_hierarchy_target_context_uses_simbad_ab_alias_and_ignores_moving_group_parent(
    session_factory,
    tmp_path,
):
    base_ra = 33.510125
    base_dec = 47.48425
    endpoint_ra = base_ra
    endpoint_dec = base_dec
    target = IdentityService(session_factory).add(AddRequest(ra_deg=endpoint_ra, dec_deg=endpoint_dec))
    value = snapshot("HD 13594", spectral_type="F1V")
    value = value.__class__(
        **{
            **value.__dict__,
            "identifiers": ("HD 13594",),
            "primary_object_type": "**",
            "relationships": (
                RelationshipValue(
                    "parent",
                    456,
                    "NAME Ursa Major Moving Group",
                    188.0,
                    57.0,
                    100,
                    "2001MNRAS.328...45M",
                    264173.6,
                    related_object_type="MGr",
                    related_object_types=("As*", "MGr"),
                ),
                RelationshipValue(
                    "child",
                    789,
                    "HD 13594A",
                    base_ra,
                    base_dec,
                    None,
                    "2001AJ....122.3466M",
                    0.08,
                    related_object_type="PM*",
                    related_object_types=("*", "**", "PM*"),
                ),
                RelationshipValue(
                    "child",
                    790,
                    "HD 13594B",
                    base_ra - 0.4 / (3600.0 * math.cos(math.radians(base_dec))),
                    base_dec,
                    None,
                    "2001AJ....122.3466M",
                    0.4,
                    related_object_type="PM*",
                    related_object_types=("*", "**", "PM*"),
                ),
            ),
        }
    )
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (value,))),
    ).refresh(target.sdbid)
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="WDS J02140+4729AB",
            normalized_value=normalize_identifier("WDS J02140+4729AB"),
            source="simbad",
        ))
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="CCDM J02140+4729AB",
            normalized_value=normalize_identifier("CCDM J02140+4729AB"),
            source="simbad",
        ))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        f"02140+4729,STF228,AB,{base_ra},{base_dec},2024,323,0.4,6.56,7.21\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)
    service.derive_graph("wds", source_id=imported.source_id)

    context = service.target_context(target.sdbid)

    assert context["semantic_identity"]["parents"][0]["component_relevance"] == "contextual_group"
    assert context["semantic_identity"]["relationship_relevance_counts"] == {
        "contextual_group": 1,
        "planetary_or_disk": 0,
        "stellar_or_substellar_component": 2,
        "unknown": 0,
    }
    assert context["semantic_identity"]["kind"] == "system_or_parent"
    assert context["semantic_identity"]["component_label_candidates"][0] == {
        "confidence": "low",
        "label": "AB",
        "source": "identifier",
        "value": "WDS J02140+4729AB",
    }
    assert context["component_assignment"]["status"] == "semantic_group_contains_nearest_component"
    assert context["component_assignment"]["semantic_component"] == "AB"
    assert context["photometry_context"]["target_level"] == "system"


def test_hierarchy_target_context_marks_nonstellar_simbad_children(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    endpoint_ra = base_ra + 3.0 / (3600.0 * math.cos(math.radians(base_dec)))
    target = IdentityService(session_factory).add(AddRequest(ra_deg=endpoint_ra, dec_deg=base_dec))
    value = snapshot("Planet host", spectral_type="G2V")
    value = value.__class__(
        **{
            **value.__dict__,
            "relationships": (
                RelationshipValue(
                    "child",
                    999,
                    "Planet host b",
                    endpoint_ra,
                    base_dec,
                    None,
                    None,
                    0.1,
                    related_object_type="Pl?",
                    related_object_types=("*", "Pl?"),
                ),
            ),
        }
    )
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (value,))),
    ).refresh(target.sdbid)
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        f"00057+4549,STF3050,AB,{base_ra},{base_dec},2024,90,3.0,7.1,8.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)
    service.derive_graph("wds", source_id=imported.source_id)

    context = service.target_context(target.sdbid)

    assert context["semantic_identity"]["children"][0]["component_relevance"] == "planetary_or_disk"
    assert context["semantic_identity"]["kind"] == "single_or_no_known_hierarchy"
    assert context["component_assignment"]["status"] == "semantic_hierarchy_not_stellar_component"


def test_hierarchy_target_context_compares_simbad_component_label_to_geometry(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    endpoint_ra = base_ra + 3.0 / (3600.0 * math.cos(math.radians(base_dec)))
    target = IdentityService(session_factory).add(AddRequest(ra_deg=endpoint_ra, dec_deg=base_dec))
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (snapshot("HD 1 C"),))),
    ).refresh(target.sdbid)
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        f"00057+4549,STF3050,AB,{base_ra},{base_dec},2024,90,3.0,7.1,8.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)
    service.derive_graph("wds", source_id=imported.source_id)

    context = service.target_context(target.sdbid)

    assert context["semantic_identity"]["component_label_candidates"][0]["label"] == "C"
    assert context["nearest_component"]["component"] == "B"
    assert context["component_assignment"]["status"] == "semantic_geometry_conflict"
    assert context["component_assignment"]["semantic_component"] == "C"


def test_hierarchy_target_context_does_not_assume_disconnected_groups_share_parent(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=82.187, dec_deg=-65.449))
    children_only = snapshot("HD 1", spectral_type="F5V")
    children_only = children_only.__class__(
        **{
            **children_only.__dict__,
            "relationships": tuple(
                relationship for relationship in children_only.relationships
                if relationship.direction == "child"
            ),
        }
    )
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (children_only,))),
    ).refresh(target.sdbid)
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "44444+4444,AAA,AB,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        "44444+4444,AAA,CD,82.187,-65.449,2024,180,6.0,8.1,9.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)
    service.derive_graph("wds", source_id=imported.source_id)

    context = service.target_context(target.sdbid)

    assert context["semantic_identity"]["kind"] == "system_or_parent"
    assert context["component_assignment"]["status"] == "ambiguous_disconnected_groups"
    assert context["component_assignment"]["confidence"] == "low"


def test_hierarchy_graph_diagnostics_report_structural_problems(session_factory, tmp_path):
    IdentityService(session_factory).add(AddRequest(ra_deg=82.187, dec_deg=-65.449))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "00057+4549,AAA,AC,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        "00057+4549,AAA,BC,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        "05287-6527,AAA,AB,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        "05287-6527,AAA,AC,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        "05287-6527,AAA,BC,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        "44444+4444,AAA,AB,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        "44444+4444,AAA,CD,82.187,-65.449,2024,90,5.0,7.1,8.2\n"
        "11111+1111,AAA,,82.187,-65.449,2024,,5.0,7.1,8.2\n"
        "22222+2222,AAA,,120.0,30.0,2024,,5.0,7.1,8.2\n"
        '33333+3333,AAA,"Aa,Ab",82.187,-65.449,2024,90,0.5,7.1,8.2\n'
        "33333+3333,AAA,AB,82.187,-65.449,2024,90,5.0,7.1,8.2\n",
        encoding="utf-8",
    )

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)
    service.derive_graph("wds", source_id=imported.source_id)
    diagnostics = service.graph_diagnostics(provider="wds", source_id=imported.source_id)
    by_issue = {(row.native_id, row.issue): row for row in diagnostics}

    assert by_issue[("00057+4549", "matched_without_structural_edges")].matched_candidate_count == 2
    assert by_issue[("00057+4549", "matched_without_structural_edges")].detail == "matched hierarchy candidates exist, but no active structural graph edge remains"
    assert ("05287-6527", "matched_without_structural_edges") not in by_issue
    assert by_issue[("44444+4444", "disconnected_structural_groups")].detail == "structural roots: A, C"
    assert by_issue[("44444+4444", "disconnected_structural_groups")].severity == "info"
    assert ("05287-6527", "duplicate_structural_parent") not in by_issue
    assert by_issue[("11111+1111", "structural_geometry_problem")].detail == "blank Comp interpreted as A->B geometry base_only; missing PA"
    assert ("22222+2222", "structural_geometry_problem") not in by_issue
    assert ("33333+3333", "disconnected_structural_groups") not in by_issue

    review_rows = service.graph_diagnostics(provider="wds", source_id=imported.source_id, severity="review", limit=0)
    assert review_rows
    assert {row.severity for row in review_rows} == {"review"}

    geometry_rows = service.graph_diagnostics(
        provider="wds",
        source_id=imported.source_id,
        issue="structural_geometry_problem",
        limit=0,
    )
    assert [row.native_id for row in geometry_rows] == ["11111+1111"]


def test_hierarchy_graph_diagnostics_handles_large_wds_snapshots(session_factory, tmp_path):
    path = tmp_path / "wds-large.csv"
    rows = [
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2",
    ]
    for index in range(1100):
        ra = 1.0 + index * 0.001
        native_id = f"{index // 60:02d}{index % 60:02d}+0000"
        rows.append(f"{native_id},AAA,AB,{ra:.6f},0.0,2024,90,1.0,7.1,8.2")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    result = service.derive_graph("wds", source_id=imported.source_id)
    diagnostics = service.graph_diagnostics(provider="wds", source_id=imported.source_id)

    assert result.edge_count == 1100
    assert diagnostics == ()


def test_hierarchy_cli_derives_lists_and_overrides_graph(tmp_path, capsys):
    from sdb_identity.cli import main

    database = tmp_path / "hierarchy-graph.sqlite"
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "05287-6527,AAA,AB,82.187,-65.449,2024,90,5.0,7.1,8.2\n",
        encoding="utf-8",
    )

    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "hierarchy", "source", "fetch",
        "wds", "--file", str(path), "--release", "test-release",
    ]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert main([
        "--database", str(database), "hierarchy", "graph", "derive",
        "wds", "--source-id", str(imported["source_id"]),
    ]) == 0
    derived = json.loads(capsys.readouterr().out)
    assert derived["edge_count"] == 1
    assert main([
        "--database", str(database), "hierarchy", "graph", "diagnostics",
        "--provider", "wds", "--source-id", str(imported["source_id"]),
    ]) == 0
    assert capsys.readouterr().out == ""
    assert main([
        "--database", str(database), "hierarchy", "graph", "list",
        "05287-6527", "--provider", "wds",
    ]) == 0
    edge = json.loads(capsys.readouterr().out)
    assert edge["reference_label"] == "A"
    assert edge["component_label"] == "B"
    assert main([
        "--database", str(database), "hierarchy", "graph", "override",
        "wds", "05287-6527", "--from", "A", "--to", "B",
        "--status", "rejected", "--role", "non_structural", "--actor", "tester", "--reason", "bad row",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "hierarchy", "graph", "list",
        "05287-6527", "--provider", "wds",
    ]) == 0
    edge = json.loads(capsys.readouterr().out)
    assert edge["status"] == "rejected"
    assert edge["structural_role"] == "non_structural"
    assert edge["override_reason"] == "bad row"
    assert main([
        "--database", str(database), "hierarchy", "graph", "diagnostics",
        "--provider", "wds", "--summary", "--severity", "info",
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "count": 1,
        "issue": "only_non_structural_edges",
        "severity": "info",
    }


def test_wds_identifier_position_is_not_used_when_component_endpoint_exists(session_factory, tmp_path):
    base = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    endpoint_ra = 1.425 + 3.0 / (3600.0 * math.cos(math.radians(45.8166667)))
    endpoint = IdentityService(session_factory).add(AddRequest(ra_deg=endpoint_ra, dec_deg=45.8166667))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "00057+4549,STF3050,AB,2024,90,3.0,7.1,8.2\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")

    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)

    with session_factory() as session:
        candidates = tuple(session.scalars(select(HierarchyMatchCandidate).order_by(HierarchyMatchCandidate.id)))
        record = session.scalar(select(HierarchyRecord))
        payload = json.loads(record.raw_payload_json)

    assert [candidate.target_id for candidate in candidates] == [endpoint.target_id]
    assert candidates[0].separation_arcsec < 0.01
    assert "component endpoint separation" in candidates[0].reason
    assert base.target_id not in [candidate.target_id for candidate in candidates]
    assert payload["rComp"] == "A"
    assert payload["Comp"] == "B"
    assert payload["coordinate_source"] == "wds_id_only"


def test_wds_vizier_sexagesimal_position_is_used_for_reference_component(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=4.5953333333, dec_deg=44.0229444444))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2\n"
        "00184+4401,GRB 34,AB,00 18 22.88,+44 01 22.6,2019,66,34.1\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")

    service.match_records("wds", source_id=imported.source_id, radius_arcsec=5.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))
        record = session.scalar(select(HierarchyRecord))
        payload = json.loads(record.raw_payload_json)

    assert record.ra_deg == pytest.approx(4.5953333333)
    assert record.dec_deg == pytest.approx(44.0229444444)
    assert payload["coordinate_source"] == "wds_catalog"
    assert payload["rComp"] == "A"
    assert payload["Comp"] == "B"
    assert candidate.target_id == target.target_id
    assert candidate.separation_arcsec < 0.01
    assert "record position separation" in candidate.reason


def test_hierarchy_cli_imports_snapshot_and_lists_sources(tmp_path, capsys):
    from sdb_identity.cli import main

    database = tmp_path / "hierarchy-import.sqlite"
    snapshot = tmp_path / "wds.csv"
    snapshot.write_text(
        "WDS,Discov,Comp,Obs1,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "00057+4549,STF3050,AB,1822,2024,134,2.345,7.1,8.2\n",
        encoding="utf-8",
    )
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()

    assert main([
        "--database", str(database), "hierarchy", "source", "fetch",
        "wds", "--file", str(snapshot), "--release", "test-release",
    ]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["provider"] == "wds"
    assert imported["row_count"] == 1

    assert main(["--database", str(database), "hierarchy", "source", "list", "--provider", "wds"]) == 0
    source = json.loads(capsys.readouterr().out)
    assert source["release"] == "test-release"


class FakeHierarchyClient:
    def fetch_tables(self, catalog):
        assert catalog in {"B/wds", "I/274"}
        table = Table()
        if catalog == "B/wds":
            table.meta["name"] = "B/wds/wds"
            table["WDS"] = ["00057+4549"]
            table["Discov"] = ["STF3050"]
            table["Comp"] = ["AB"]
            table["Obs1"] = [1822]
            table["Obs2"] = [2024]
            table["PA2"] = [134]
            table["Sep2"] = [2.345]
            table["Mag1"] = [7.1]
            table["Mag2"] = [8.2]
            table["Notes"] = [""]
        else:
            table.meta["name"] = "I/274/ccdm"
            table["CCDM"] = ["J01553-6019AB"]
            table["Comp"] = ["AB"]
            table["RAJ2000"] = [28.875]
            table["DEJ2000"] = [-60.3167]
            table["Mag1"] = [6.1]
            table["Mag2"] = [7.4]
        return [table]

    def fetch_readme(self, catalog):
        return f"{catalog} test ReadMe\nLast update: 2026-07-01\n"

    def source_url(self, catalog):
        return f"https://example.test/{catalog}"


def test_hierarchy_fetches_vizier_wds_snapshot_with_version_note(session_factory):
    result = HierarchyService(session_factory).fetch_snapshot(
        "wds", client=FakeHierarchyClient(),
    )

    with session_factory() as session:
        source = session.get(HierarchySource, result.source_id)
        record = session.scalar(select(HierarchyRecord).where(HierarchyRecord.source_id == result.source_id))

    assert result.provider == "wds"
    assert result.release == "wds:B/wds:2026-07-01"
    assert source.source_file == "https://example.test/B/wds"
    assert source.fetched_at is not None
    assert "Last update" in source.note
    assert record.native_id == "00057+4549"
    assert record.separation_arcsec == 2.345


def test_hierarchy_fetches_vizier_ccdm_snapshot(session_factory):
    result = HierarchyService(session_factory).fetch_snapshot(
        "ccdm", client=FakeHierarchyClient(), release="manual-release",
    )

    with session_factory() as session:
        record = session.scalar(select(HierarchyRecord).where(HierarchyRecord.source_id == result.source_id))

    assert result.release == "manual-release"
    assert record.provider == "ccdm"
    assert record.native_id == "J01553-6019AB"
    assert record.delta_mag == 1.3


def test_hierarchy_matches_wds_record_by_existing_identifier(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="WDS 00057+4549",
            normalized_value=normalize_identifier("WDS 00057+4549"),
            source="simbad",
        ))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,PA2,Sep2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,90,2.345\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")

    result = service.match_records("wds", source_id=imported.source_id, radius_arcsec=10.0)
    review = service.review_matches("wds")

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    assert result.record_count == 1
    assert result.candidate_count == 1
    assert candidate.target_id == target.target_id
    assert candidate.match_method == "identifier+position"
    assert candidate.score == 1.0
    assert "identifier match" in candidate.reason
    assert review[0].sdbid == target.sdbid
    assert review[0].native_id == "00057+4549"


def test_hierarchy_identifier_match_does_not_claim_distant_position(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="WDS 00057+4549",
            normalized_value=normalize_identifier("WDS 00057+4549"),
            source="simbad",
        ))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Sep2\n"
        "00057+4549,STF3050,AB,10.0,45.8166667,2.345\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")

    service.match_records("wds", source_id=imported.source_id, radius_arcsec=10.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    assert candidate.target_id == target.target_id
    assert candidate.match_method == "identifier"
    assert candidate.separation_arcsec > 10
    assert "record position offset" in candidate.reason


def test_hierarchy_matches_ccdm_record_by_position_without_identifier(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=28.8751, dec_deg=-60.3167))
    path = tmp_path / "ccdm.tsv"
    path.write_text(
        "CCDM\tComp\tRAJ2000\tDEJ2000\tMag1\tMag2\n"
        "J01553-6019AB\tAB\t28.875\t-60.3167\t6.1\t7.4\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("ccdm", path, release="test-ccdm")

    result = service.match_records("ccdm", source_id=imported.source_id, radius_arcsec=1.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    assert result.candidate_count == 1
    assert candidate.target_id == target.target_id
    assert candidate.match_method == "position"
    assert 0 < candidate.score < 1.0
    assert candidate.separation_arcsec < 1.0


def test_hierarchy_matches_ccdm_component_by_existing_wds_alias(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.575, dec_deg=58.4333333))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="WDS J00063+5826AB",
            normalized_value=normalize_identifier("WDS J00063+5826AB"),
            source="simbad",
        ))
    path = tmp_path / "ccdm.dat.gz"
    row = (
        " 00063+5826 B  STF3062                   1831             02  8.1 G0 "
        "                                                       00063N5826B       \n"
    )
    path.write_bytes(gzip.compress(row.encode()))
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("ccdm", path, release="test-ccdm")

    service.match_records("ccdm", source_id=imported.source_id, radius_arcsec=30.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    assert candidate.target_id == target.target_id
    assert candidate.match_method == "identifier"
    assert "WDS J00063+5826AB" in candidate.reason
    assert "coarse CCDM identifier position" not in candidate.reason


def test_hierarchy_matches_ccdm_ab_alias_to_both_a_and_b_components(
    session_factory,
    tmp_path,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=9.3362, dec_deg=-24.7673))
    with session_factory.begin() as session:
        session.add_all([
            ExternalIdentifier(
                target_id=target.target_id,
                value="CCDM J00373-2446AB",
                normalized_value=normalize_identifier("CCDM J00373-2446AB"),
                source="simbad",
            ),
            ExternalIdentifier(
                target_id=target.target_id,
                value="WDS J00373-2446AB",
                normalized_value=normalize_identifier("WDS J00373-2446AB"),
                source="simbad",
            ),
        ])
    path = tmp_path / "ccdm.tsv"
    path.write_text(
        "CCDM\tComp\tDisc\tRAJ2000\tDEJ2000\n"
        "00373-2446\tA\t\t9.3362\t-24.7673\n"
        "00373-2446\tB\tBU  395\t9.3362\t-24.7673\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("ccdm", path, release="test-ccdm")

    service.match_records("ccdm", source_id=imported.source_id, radius_arcsec=1.0)

    with session_factory() as session:
        rows = list(session.execute(
            select(HierarchyMatchCandidate, HierarchyRecord)
            .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
            .order_by(HierarchyRecord.component)
        ))

    assert [(record.component, candidate.match_method) for candidate, record in rows] == [
        ("A", "identifier+position"),
        ("B", "identifier+position"),
    ]
    for candidate, _record in rows:
        assert candidate.identifier == "CCDM J00373-2446AB"
        assert "identifier match: CCDM J00373-2446AB" in candidate.reason
        assert "identifier match: WDS J00373-2446AB" in candidate.reason


def test_hierarchy_matches_blank_wds_pair_by_implied_ab_alias(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="WDS J00057+4549AB",
            normalized_value=normalize_identifier("WDS J00057+4549AB"),
            source="simbad",
        ))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\t\t1.425\t45.8166667\t2024\t90\t3.0\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-wds")

    service.match_records("wds", source_id=imported.source_id, radius_arcsec=5.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    assert candidate.target_id == target.target_id
    assert candidate.match_method == "identifier+position"
    assert candidate.identifier == "WDS J00057+4549AB"
    assert "identifier match: WDS J00057+4549AB" in candidate.reason


def test_hierarchy_does_not_imply_blank_wds_ab_alias_when_explicit_ab_exists(
    session_factory,
    tmp_path,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="WDS J00057+4549AB",
            normalized_value=normalize_identifier("WDS J00057+4549AB"),
            source="simbad",
        ))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\t\t1.425\t45.8166667\t2024\t90\t3.0\n"
        "00057+4549\tSTF3050\tAB\t1.425\t45.8166667\t2024\t45\t0.2\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-wds")

    service.match_records("wds", source_id=imported.source_id, radius_arcsec=5.0)

    with session_factory() as session:
        rows = list(session.execute(
            select(HierarchyMatchCandidate, HierarchyRecord)
            .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
            .order_by(HierarchyRecord.component)
        ))

    blank_candidate = next(candidate for candidate, record in rows if not record.component)
    explicit_candidate = next(candidate for candidate, record in rows if record.component == "AB")
    assert blank_candidate.match_method == "position"
    assert blank_candidate.identifier is None
    assert explicit_candidate.match_method == "identifier+position"
    assert explicit_candidate.identifier == "WDS J00057+4549AB"


def test_hierarchy_matches_subsystem_component_to_parent_alias(
    session_factory,
    tmp_path,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="WDS J00057+4549A",
            normalized_value=normalize_identifier("WDS J00057+4549A"),
            source="simbad",
        ))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\tAa,Ab\t1.425\t45.8166667\t2024\t90\t0.2\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-wds")

    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    assert candidate.target_id == target.target_id
    assert candidate.match_method == "identifier+position"
    assert candidate.identifier == "WDS J00057+4549A"
    assert "identifier match: WDS J00057+4549A" in candidate.reason


def test_hierarchy_does_not_match_cross_group_component_to_leaf_alias(
    session_factory,
    tmp_path,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="WDS J00057+4549A",
            normalized_value=normalize_identifier("WDS J00057+4549A"),
            source="simbad",
        ))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\tAB,C\t1.425\t45.8166667\t2024\t90\t3.0\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-wds")

    service.match_records("wds", source_id=imported.source_id, radius_arcsec=1.0)

    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    assert candidate.match_method == "position"
    assert candidate.identifier is None
    assert "identifier match" not in candidate.reason


def test_hierarchy_cli_matches_and_reviews_snapshot(tmp_path, capsys):
    from sdb_identity.cli import main

    database = tmp_path / "hierarchy-match.sqlite"
    snapshot = tmp_path / "wds.csv"
    snapshot.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Sep2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,2.345\n",
        encoding="utf-8",
    )
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "add", "--ra", "1.425", "--dec", "45.8166667",
    ]) == 0
    target = json.loads(capsys.readouterr().out)
    assert main([
        "--database", str(database), "hierarchy", "source", "fetch",
        "wds", "--file", str(snapshot), "--release", "test-release",
    ]) == 0
    imported = json.loads(capsys.readouterr().out)

    assert main([
        "--database", str(database), "hierarchy", "match",
        "wds", "--source-id", str(imported["source_id"]), "--radius", "10",
    ]) == 0
    matched = json.loads(capsys.readouterr().out)
    assert matched["candidate_count"] == 1

    assert main(["--database", str(database), "hierarchy", "candidates", "--provider", "wds"]) == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["sdbid"] == target["sdbid"]
    assert reviewed["match_method"] == "position"


def test_hierarchy_summary_reports_sources_and_candidate_counts(session_factory, tmp_path):
    IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,PA2,Sep2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,90,2.345\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=10.0)
    service.derive_graph("wds", source_id=imported.source_id)
    other_path = tmp_path / "wds-other.csv"
    other_path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,PA2,Sep2\n"
        "00058+4549,STF3051,AB,1.525,45.8166667,90,2.345\n",
        encoding="utf-8",
    )
    other = service.import_snapshot("wds", other_path, release="other-release")

    summary = service.summary("wds", source_id=imported.source_id)

    assert summary["source_id"] == imported.source_id
    assert [source["source_id"] for source in summary["sources"]] == [imported.source_id]
    assert summary["sources"][0]["record_count"] == 1
    assert summary["record_counts"][0]["record_count"] == 1
    assert summary["record_counts"][0]["matched_records"] == 1
    assert summary["record_counts"][0]["unmatched_records"] == 0
    assert summary["candidate_status_counts"] == [
        {"provider": "wds", "status": "candidate", "count": 1},
    ]
    assert summary["candidate_method_counts"] == [
        {"provider": "wds", "match_method": "position", "count": 1},
    ]
    assert summary["graph_relation_counts"] == [
        {"provider": "wds", "relation_type": "group", "count": 1},
    ]
    assert summary["graph_status_counts"] == [
        {"provider": "wds", "status": "derived", "count": 1},
    ]
    assert summary["graph_geometry_counts"] == [
        {"provider": "wds", "geometry_status": "usable", "count": 1},
    ]
    assert summary["graph_role_counts"] == [
        {"provider": "wds", "structural_role": "structural", "count": 1},
    ]
    assert service.summary("wds", source_id=other.source_id)["candidate_status_counts"] == []


def test_hierarchy_cli_summary_reports_match_workload(tmp_path, capsys):
    from sdb_identity.cli import main

    database = tmp_path / "hierarchy-summary.sqlite"
    snapshot = tmp_path / "wds.csv"
    snapshot.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,PA2,Sep2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,90,2.345\n",
        encoding="utf-8",
    )
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "add", "--ra", "1.425", "--dec", "45.8166667",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "hierarchy", "source", "fetch",
        "wds", "--file", str(snapshot), "--release", "test-release",
    ]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert main([
        "--database", str(database), "hierarchy", "match",
        "wds", "--source-id", str(imported["source_id"]), "--radius", "10",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "hierarchy", "graph", "derive",
        "wds", "--source-id", str(imported["source_id"]),
    ]) == 0
    capsys.readouterr()

    assert main([
        "--database", str(database), "hierarchy", "summary",
        "--provider", "wds", "--source-id", str(imported["source_id"]),
    ]) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["record_counts"][0]["matched_records"] == 1
    assert summary["candidate_status_counts"][0]["count"] == 1
    assert summary["graph_relation_counts"][0]["relation_type"] == "group"
    assert summary["graph_geometry_counts"][0]["geometry_status"] == "usable"
    assert summary["graph_role_counts"][0]["structural_role"] == "structural"


def test_hierarchy_accepts_candidate_with_system_and_relationship(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,2024,134,2.345\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=10.0)
    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    result = service.accept_match(
        candidate.id,
        actor="tester",
        reason="looks right",
        system="test WDS system",
        relationship_type="pair",
    )

    with session_factory() as session:
        accepted = session.get(HierarchyMatchCandidate, candidate.id)
        action = session.scalar(select(HierarchyMatchAction))
        relationship = session.get(StructuralEdge, result.relationship_id)
        system = session.get(TargetSystem, result.system_id)

    assert result.new_status == "accepted"
    assert accepted.status == "accepted"
    assert action.action == "accept"
    assert action.previous_status == "candidate"
    assert action.relationship_id == relationship.id
    assert relationship.source == "wds"
    assert relationship.status == "accepted"
    assert relationship.record_id is not None
    assert relationship.endpoint_a_target_id == target.target_id
    assert relationship.separation_arcsec == 2.345
    assert relationship.pa_deg == 134
    assert relationship.relation_epoch == 2024
    assert system.name == "test WDS system"
    assert {item.sdbid for item, _count, _created in pending_export_targets(session_factory)} == {
        target.sdbid,
    }


def test_structural_edges_discriminate_derived_from_accepted(session_factory, tmp_path):
    """One structural_edges table holds both derived graph edges and accepted
    relationships; graph readers must see only the former and relationship
    readers only the latter, and re-deriving must leave accepted rows alone."""
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,2024,134,2.345\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("wds", path, release="test-release")
    service.match_records("wds", source_id=imported.source_id, radius_arcsec=10.0)
    service.derive_graph("wds", source_id=imported.source_id)
    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    derived_before = service.graph_edges(
        provider="wds", native_id="00057+4549", source_id=imported.source_id
    )
    assert derived_before
    assert all(row.status in ("derived", "stale", "rejected") for row in derived_before)

    result = service.accept_match(
        candidate.id, actor="tester", reason="physical", relationship_type="pair"
    )

    # Graph readers ignore the accepted relationship even though it shares the
    # same source/record; the derived rows are unchanged.
    derived_after = service.graph_edges(
        provider="wds", native_id="00057+4549", source_id=imported.source_id
    )
    assert {row.edge_id for row in derived_after} == {row.edge_id for row in derived_before}
    assert result.relationship_id not in {row.edge_id for row in derived_after}

    # Relationship readers see only the accepted row, none of the derived edges.
    relationships = service.status(target.sdbid).relationships
    assert len(relationships) == 1
    assert relationships[0].id == result.relationship_id
    assert relationships[0].status == "accepted"

    # Re-deriving must not touch the accepted row.
    with session_factory() as session:
        accepted_before = session.get(StructuralEdge, result.relationship_id)
        accepted_status_before = accepted_before.status
    service.derive_graph("wds", source_id=imported.source_id)
    with session_factory() as session:
        accepted_after = session.get(StructuralEdge, result.relationship_id)
        graph_count = session.scalar(
            select(func.count(StructuralEdge.id)).where(
                StructuralEdge.status.in_(("derived", "stale", "rejected"))
            )
        )
    assert accepted_after is not None
    assert accepted_after.status == accepted_status_before == "accepted"
    assert graph_count == len(derived_before)


def test_hierarchy_rejects_candidate_with_audit(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=28.8751, dec_deg=-60.3167))
    path = tmp_path / "ccdm.tsv"
    path.write_text(
        "CCDM\tComp\tRAJ2000\tDEJ2000\n"
        "J01553-6019AB\tAB\t28.875\t-60.3167\n",
        encoding="utf-8",
    )
    service = HierarchyService(session_factory)
    imported = service.import_snapshot("ccdm", path, release="test-ccdm")
    service.match_records("ccdm", source_id=imported.source_id, radius_arcsec=1.0)
    with session_factory() as session:
        candidate = session.scalar(select(HierarchyMatchCandidate))

    result = service.reject_match(candidate.id, actor="tester", reason="wrong component")

    with session_factory() as session:
        rejected = session.get(HierarchyMatchCandidate, candidate.id)
        action = session.scalar(select(HierarchyMatchAction))

    assert result.new_status == "rejected"
    assert rejected.status == "rejected"
    assert action.action == "reject"
    assert action.reason == "wrong component"
    assert action.relationship_id is None
    assert {item.sdbid for item, _count, _created in pending_export_targets(session_factory)} == {
        target.sdbid,
    }


def test_hierarchy_cli_accepts_and_rejects_candidates(tmp_path, capsys):
    from sdb_identity.cli import main

    database = tmp_path / "hierarchy-actions.sqlite"
    snapshot = tmp_path / "wds.csv"
    snapshot.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Sep2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,2.345\n",
        encoding="utf-8",
    )
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "add", "--ra", "1.425", "--dec", "45.8166667",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "hierarchy", "source", "fetch",
        "wds", "--file", str(snapshot), "--release", "test-release",
    ]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert main([
        "--database", str(database), "hierarchy", "match",
        "wds", "--source-id", str(imported["source_id"]), "--radius", "10",
    ]) == 0
    capsys.readouterr()
    assert main(["--database", str(database), "hierarchy", "candidates", "--provider", "wds"]) == 0
    candidate = json.loads(capsys.readouterr().out)

    assert main([
        "--database", str(database), "hierarchy", "accept-candidate",
        str(candidate["candidate_id"]), "--actor", "tester", "--reason", "cli accept",
        "--system", "cli WDS system", "--type", "pair",
    ]) == 0
    accepted = json.loads(capsys.readouterr().out)

    assert accepted["new_status"] == "accepted"
    assert accepted["relationship_id"] is not None
    assert accepted["system_id"] is not None
