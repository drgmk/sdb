from __future__ import annotations

from astropy.table import Table
from sqlalchemy import select

from sdb_identity.catalogs.acquisition import CatalogAcquisitionService
from sdb_identity.catalogs.types import CatalogCandidate, MeasurementValue
from sdb_identity.catalogs.policy import catalog_band_wavelength_micron
from sdb_identity.assignment_proposals import measurement_assignment_proposals
from sdb_identity.assignment_review import build_measurement_assignment_review
from sdb_identity.astrometry import propagate_to_epoch
from sdb_identity.cli import main
from sdb_identity.database import init_database, make_session_factory
from sdb_identity.export import export_ipac
from sdb_identity.hierarchy import HierarchyService
from sdb_identity.models.catalogs import (
    CatalogDetection,
    NormalizedMeasurement,
    RawCatalogRow,
)
from sdb_identity.models.identity import ExternalIdentifier
from sdb_identity.models.photometry import (
    MeasurementAssociationAction,
    MeasurementTargetAssociation,
)
from sdb_identity.photometry import (
    assign_measurement_target,
    list_measurement_assignment_history,
    list_measurement_target_assignments,
    set_measurement_eligibility,
    unassign_measurement_target,
)
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.identifiers import normalize_identifier
from sdb_identity.proposal_application import apply_measurement_assignment_proposals
from sdb_identity.review_actions import review_catalog_target_association_decision
from sdb_identity.samples import SampleService
from sdb_identity.target_lifecycle import (
    set_target_lifecycle,
    target_lifecycle_history,
    target_lifecycle_status,
)
from tests.test_catalog import FakeCatalog, candidate, measurement
from tests.fakes import FakeSimbad, astrometry, simbad_result


def _wise_catalog():
    wise = MeasurementValue(
        band="WISE3P4", value=7.2, error=0.02, systematic_error=0.01,
        unit="mag", bibcode="test", resolution_major_arcsec=6.1,
        resolution_minor_arcsec=6.1, resolution_kind="fwhm",
        resolution_reference="test",
    )
    return FakeCatalog(
        [candidate("joint-wise", measurements=[wise])],
        name="allwise",
        release="test",
        query_epoch=2010.5,
    )


def _configured_system(session_factory, *, half_separation_deg=0.0003):
    identity = IdentityService(session_factory)
    system = identity.add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    component_a = identity.add(AddRequest(
        ra_deg=10.0 - half_separation_deg, dec_deg=-20.0,
    ))
    component_b = identity.add(AddRequest(
        ra_deg=10.0 + half_separation_deg, dec_deg=-20.0,
    ))
    hierarchy = HierarchyService(session_factory)
    hierarchy.create_system("proposal AB", primary=system.sdbid)
    hierarchy.add_member("proposal AB", component_a.sdbid, component_label="A")
    hierarchy.add_member("proposal AB", component_b.sdbid, component_label="B")
    set_target_lifecycle(
        session_factory, system.sdbid, role="composite", state="system_only",
        actor="test", reason="AB composite",
    )
    for target, component in ((component_a, "A"), (component_b, "B")):
        set_target_lifecycle(
            session_factory, target.sdbid, role="physical", state="active",
            actor="test", reason=f"physical component {component}",
        )
    return system, component_a, component_b


def _move_catalog_association(session_factory, *, source, destination):
    with session_factory() as session:
        raw = session.query(RawCatalogRow).one()
        detection_id = raw.detection_id
        raw_id = raw.id
    for target, action in ((source, "reject"), (destination, "accept")):
        preview = review_catalog_target_association_decision(
            session_factory,
            target_reference=target.sdbid,
            detection_id=detection_id,
            action=action,
            reviewed_raw_row_id=raw_id,
        )
        review_catalog_target_association_decision(
            session_factory,
            target_reference=target.sdbid,
            detection_id=detection_id,
            action=action,
            reviewed_raw_row_id=raw_id,
            apply=True,
            actor="reviewer",
            reason="exact catalog identifier belongs to the physical component",
            expected_token=preview["state_token"],
        )


def test_target_lifecycle_defaults_and_append_only_changes(session_factory):
    identity = IdentityService(session_factory)
    system = identity.add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    primary = identity.add(AddRequest(ra_deg=10.01, dec_deg=-20.0))

    assert target_lifecycle_status(session_factory, system.sdbid).role == "unspecified"
    assert target_lifecycle_status(session_factory, system.sdbid).state == "active"
    set_target_lifecycle(
        session_factory, system.sdbid,
        role="composite", state="system_only", actor="reviewer",
        reason="AB is a measurement scope rather than a third photosphere",
    )
    set_target_lifecycle(
        session_factory, system.sdbid,
        role="composite", state="superseded", superseded_by=primary.sdbid,
        actor="reviewer", reason="resolved components are the active targets",
    )

    status = target_lifecycle_status(session_factory, system.sdbid)
    assert (status.role, status.state) == ("composite", "superseded")
    assert status.superseded_by_sdbid == primary.sdbid
    assert len(target_lifecycle_history(session_factory, system.sdbid)) == 2


def test_measurement_contributors_are_many_to_many_and_audited_without_changing_export(
    session_factory,
    tmp_path,
):
    identity = IdentityService(session_factory)
    system = identity.add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    component_a = identity.add(AddRequest(ra_deg=10.01, dec_deg=-20.0))
    component_b = identity.add(AddRequest(ra_deg=10.02, dec_deg=-20.0))
    hierarchy = HierarchyService(session_factory)
    hierarchy.create_system("test AB", primary=system.sdbid)
    hierarchy.add_member("test AB", component_a.sdbid, component_label="A")
    hierarchy.add_member("test AB", component_b.sdbid, component_label="B")
    CatalogAcquisitionService(session_factory, {"allwise": _wise_catalog()}).refresh(
        system.sdbid, "allwise"
    )
    before = Table.read(
        export_ipac(session_factory, system.sdbid, tmp_path / "before.txt"),
        format="ascii.ipac",
    )
    review = __import__("sdb_identity.photometry", fromlist=["review_photometry_associations"])
    measurement_id = review.review_photometry_associations(
        session_factory, system.sdbid
    )[0].measurement_id

    assign_measurement_target(
        session_factory, measurement_id, component_a.sdbid,
        actor="reviewer", reason="WISE contains A and B",
    )
    assign_measurement_target(
        session_factory, measurement_id, component_b.sdbid,
        actor="reviewer", reason="WISE contains A and B",
    )
    assignments_a = list_measurement_target_assignments(
        session_factory, component_a.sdbid
    )
    assert assignments_a[0]["measurement_id"] == measurement_id
    assert assignments_a[0]["origin_target_id"] == system.target_id
    assert assignments_a[0]["role"] == "contributor"
    context = hierarchy.system_context(system.sdbid)
    assignment = next(
        value for value in context["measurement_assignments"]
        if value["measurement_id"] == measurement_id
    )
    assert {value["sdbid"] for value in assignment["contributors"]} == {
        component_a.sdbid, component_b.sdbid,
    }
    assert set(context["target_lifecycle_by_target"]) >= {
        system.sdbid, component_a.sdbid, component_b.sdbid,
    }

    unassign_measurement_target(
        session_factory, measurement_id, component_b.sdbid,
        actor="reviewer", reason="remove provisional B assignment",
    )
    history_b = list_measurement_assignment_history(
        session_factory, component_b.sdbid
    )
    assert [value.action for value in history_b] == ["assign", "unassign"]
    assert list_measurement_target_assignments(session_factory, component_b.sdbid) == []

    after = Table.read(
        export_ipac(session_factory, system.sdbid, tmp_path / "after.txt"),
        format="ascii.ipac",
    )
    assert list(after["Band"]) == list(before["Band"])
    assert list(after["Phot"]) == list(before["Phot"])


def test_cli_records_lifecycle_and_measurement_assignments(tmp_path, capsys):
    database = tmp_path / "sdb.sqlite"
    init_database(database)
    sessions = make_session_factory(database)
    identity = IdentityService(sessions)
    origin = identity.add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    component = identity.add(AddRequest(ra_deg=10.01, dec_deg=-20.0))
    CatalogAcquisitionService(sessions, {"allwise": _wise_catalog()}).refresh(origin.sdbid, "allwise")
    review = __import__("sdb_identity.photometry", fromlist=["review_photometry_associations"])
    measurement_id = review.review_photometry_associations(sessions, origin.sdbid)[0].measurement_id
    common = ["--database", str(database)]

    assert main([
        *common, "hierarchy", "set-target-state", origin.sdbid,
        "--role", "composite", "--state", "system_only",
        "--actor", "reviewer", "--reason", "AB scope",
    ]) == 0
    assert '"role": "composite"' in capsys.readouterr().out
    assert main([
        *common, "photometry", "assign", str(measurement_id), component.sdbid,
        "--actor", "reviewer", "--reason", "joint WISE measurement",
    ]) == 0
    capsys.readouterr()
    assert main([
        *common, "photometry", "fitting-groups", "--view", "assignments", component.sdbid,
    ]) == 0
    assert f'"measurement_id": {measurement_id}' in capsys.readouterr().out


def test_blended_system_proposal_includes_physical_components_and_composite_scope(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    CatalogAcquisitionService(session_factory, {"allwise": _wise_catalog()}).refresh(
        system.sdbid, "allwise"
    )

    proposals = measurement_assignment_proposals(session_factory, system.sdbid)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["predicted_scope"] == "system"
    assert proposal["proposal_confidence"] == "medium"
    assert proposal["comparison_to_current"] == "partial_proposal"
    assert {
        (row["sdbid"], row["role"])
        for row in proposal["proposed_assignments"]
    } == {
        (system.sdbid, "composite_scope"),
        (component_a.sdbid, "contributor"),
        (component_b.sdbid, "contributor"),
    }
    context = HierarchyService(session_factory).system_context(system.sdbid)
    assert "measurement_assignment_proposals" not in context
    assert "measurement_assignment_matrix" not in context
    review = build_measurement_assignment_review(
        session_factory,
        system.sdbid,
        system_context=context,
    )
    assert review.proposals == proposals
    matrix = review.matrix
    assert [column["label"] for column in matrix["columns"]] == [
        "A", "B", "proposal AB",
    ]
    assert [column["role"] for column in matrix["columns"]] == [
        "physical", "physical", "composite",
    ]
    assert matrix["summary"] == {
        "target_count": 3,
        "measurement_count": 1,
        "band_count": 1,
        "stored_measurement_count": 1,
        "encounter_count": 1,
        "duplicate_measurement_group_count": 0,
        "comparison_counts": {"partial_proposal": 1},
        "review_required": 1,
    }
    row = matrix["rows"][0]
    assert (row["provider"], row["band"], row["predicted_scope"]) == (
        "allwise", "WISE3P4", "system",
    )
    assert row["band_count"] == 1
    assert [band["band"] for band in row["bands"]] == ["WISE3P4"]
    assert row["mixed_band_assignments"] is False
    assert {
        (cell["sdbid"], cell["status"], tuple(cell["proposed_roles"]))
        for cell in row["cells"]
    } == {
        (component_a.sdbid, "proposed", ("contributor",)),
        (component_b.sdbid, "proposed", ("contributor",)),
        (system.sdbid, "agrees", ("composite_scope",)),
    }


def test_system_matrix_consolidates_duplicate_catalog_detection_for_review(
    session_factory,
):
    system, component_a, _component_b = _configured_system(session_factory)
    service = CatalogAcquisitionService(session_factory, {"allwise": _wise_catalog()})
    service.refresh(system.sdbid, "allwise")
    service.refresh(component_a.sdbid, "allwise")

    with session_factory() as session:
        assert session.query(CatalogDetection).count() == 1
        assert session.query(NormalizedMeasurement).count() == 1
        assert session.query(RawCatalogRow).count() == 2

    matrix = build_measurement_assignment_review(
        session_factory, system.sdbid,
    ).matrix

    assert matrix["summary"]["stored_measurement_count"] == 1
    assert matrix["summary"]["encounter_count"] == 2
    assert matrix["summary"]["measurement_count"] == 1
    assert matrix["summary"]["duplicate_measurement_group_count"] == 0
    assert matrix["summary"]["review_required"] == 0
    row = matrix["rows"][0]
    assert row["stored_measurement_count"] == 1
    assert len(row["measurement_ids"]) == 1
    assert set(row["encounter_sdbids"]) == {system.sdbid, component_a.sdbid}
    assert row["duplicate_proposal_conflict"] is False
    assert row["comparison_to_current"] == "unassigned"


def test_system_matrix_collapses_detection_bands_and_marks_mixed_assignments(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    measurements = [
        MeasurementValue(
            band=band,
            value=value,
            error=0.02,
            systematic_error=0.01,
            unit="mag",
            bibcode="test",
            resolution_major_arcsec=6.1,
            resolution_minor_arcsec=6.1,
            resolution_kind="fwhm",
            resolution_reference="test",
        )
        for band, value in (("WISE3P4", 7.2), ("WISE22", 6.1))
    ]
    CatalogAcquisitionService(session_factory, {
        "allwise": FakeCatalog(
            [candidate("joint-wise", measurements=measurements)],
            name="allwise",
            release="test",
            query_epoch=2010.5,
        ),
    }).refresh(system.sdbid, "allwise")
    with session_factory() as session:
        wise3p4 = session.query(NormalizedMeasurement).filter_by(
            band="WISE3P4"
        ).one()
    assign_measurement_target(
        session_factory,
        wise3p4.id,
        component_a.sdbid,
        role="contributor",
        method="fixture",
        actor="test",
        reason="make band ownership intentionally mixed",
    )

    matrix = build_measurement_assignment_review(
        session_factory, system.sdbid,
    ).matrix

    assert matrix["summary"]["measurement_count"] == 1
    assert matrix["summary"]["band_count"] == 2
    assert matrix["summary"]["stored_measurement_count"] == 2
    assert len(matrix["rows"]) == 1
    row = matrix["rows"][0]
    assert row["band_count"] == 2
    assert [band["band"] for band in row["bands"]] == [
        "WISE3P4", "WISE22",
    ]
    assert [band["wavelength_micron"] for band in row["bands"]] == [
        3.3792, 22.2533,
    ]
    assert row["mixed_band_assignments"] is True
    assert row["comparison_to_current"] == "mixed_band_assignments"
    component_a_cell = next(
        cell for cell in row["cells"]
        if cell["sdbid"] == component_a.sdbid
    )
    assert component_a_cell["status"] == "differs"
    assert component_a_cell["mixed_band_assignments"] is True
    assert component_a_cell["band_statuses"] == {
        "WISE22": "proposed",
        "WISE3P4": "agrees",
    }
    component_b_cell = next(
        cell for cell in row["cells"]
        if cell["sdbid"] == component_b.sdbid
    )
    assert component_b_cell["status"] == "proposed"
    assert component_b_cell["mixed_band_assignments"] is False


def test_catalog_band_wavelengths_cover_read_modes_and_submm_bands():
    assert catalog_band_wavelength_micron("gaia_dr3", "GAIA.BP") == 0.5129
    assert catalog_band_wavelength_micron("2mass", "2MR2KS") == 2.1621
    assert catalog_band_wavelength_micron("iras_psc", "IRAS60") == 59.3524
    assert catalog_band_wavelength_micron("submm_obs", "WAV850") == 850.0
    assert catalog_band_wavelength_micron("unknown", "mystery") is None


def test_system_matrix_orders_catalog_detections_by_wavelength(session_factory):
    system, _component_a, _component_b = _configured_system(session_factory)
    for provider, band in (
        ("allwise", "WISE3P4"),
        ("2mass", "2MJ"),
        ("hip2", "HP"),
        ("gaia_dr3", "GAIA.BP"),
    ):
        value = MeasurementValue(
            band=band,
            value=7.0,
            error=0.02,
            unit="mag",
            resolution_major_arcsec=1.0,
            resolution_minor_arcsec=1.0,
            resolution_kind="test",
            resolution_reference="test",
        )
        CatalogAcquisitionService(session_factory, {
            provider: FakeCatalog(
                [candidate(f"{provider}-source", measurements=[value])],
                name=provider,
                release=f"test-{provider}",
            ),
        }).refresh(system.sdbid, provider)

    matrix = build_measurement_assignment_review(
        session_factory, system.sdbid,
    ).matrix

    assert [row["provider"] for row in matrix["rows"]] == [
        "gaia_dr3", "hip2", "2mass", "allwise",
    ]
    assert [row["wavelength_micron"] for row in matrix["rows"]] == [
        0.5129, 0.5420, 1.2376, 3.3792,
    ]


def test_resolved_source_between_two_components_remains_review_required(
    session_factory,
):
    system, component_a, component_b = _configured_system(
        session_factory, half_separation_deg=0.00012,
    )
    resolved = MeasurementValue(
        band="2MJ", value=7.1, error=0.02, systematic_error=0.01,
        unit="mag", bibcode="test", resolution_major_arcsec=0.1,
        resolution_minor_arcsec=0.1, resolution_kind="test",
        resolution_reference="test",
    )
    adapter = FakeCatalog(
        [candidate("central-2mass", measurements=[resolved])],
        name="2mass",
        release="test",
        query_epoch=1999.3,
    )
    CatalogAcquisitionService(session_factory, {"2mass": adapter}).refresh(system.sdbid, "2mass")

    proposal = measurement_assignment_proposals(session_factory, system.sdbid)[0]

    assert proposal["predicted_scope"] == "component"
    assert proposal["proposed_assignments"] == []
    assert proposal["comparison_to_current"] == "review_required"
    assert "multiple physical targets" in proposal["proposal_reason"]


def test_exact_simbad_identifier_wins_over_nearest_component_position(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    source_id = "00400007-2000000"
    simbad_id = f"2MASS J{source_id}"
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=component_a.target_id,
            value=simbad_id,
            normalized_value=normalize_identifier(simbad_id),
            source="simbad",
        ))
    resolved = MeasurementValue(
        band="2MJ", value=7.1, error=0.02, systematic_error=0.01,
        unit="mag", bibcode="test", resolution_major_arcsec=0.1,
        resolution_minor_arcsec=0.1, resolution_kind="test",
        resolution_reference="test",
    )
    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([
        candidate(
            source_id,
            ra=10.0003,
            dec=-20.0,
            measurements=[resolved],
        )
    ])}).refresh(system.sdbid, "2mass")

    proposal = measurement_assignment_proposals(session_factory, system.sdbid)[0]

    assert proposal["proposal_confidence"] == "high"
    assert proposal["proposed_assignments"] == [{
        "target_id": component_a.target_id,
        "sdbid": component_a.sdbid,
        "role": "contributor",
        "evidence": "simbad_identifier",
        "identifier_match": True,
        "identifier_preferred": True,
        "identifier_sources": ["simbad"],
        "separation_arcsec": proposal["proposed_assignments"][0]["separation_arcsec"],
    }]
    assert proposal["proposed_assignments"][0]["separation_arcsec"] > 1.0
    nearest = min(proposal["candidate_targets"], key=lambda row: row["separation_arcsec"])
    assert nearest["sdbid"] == component_b.sdbid


def test_snapshot_payload_identifiers_drive_hip2_and_paunzen_assignments(
    session_factory,
):
    system, component_a, _component_b = _configured_system(session_factory)
    with session_factory.begin() as session:
        for value in ("HIP 36948", "TYC 7109-2638-1"):
            session.add(ExternalIdentifier(
                target_id=component_a.target_id,
                value=value,
                normalized_value=normalize_identifier(value),
                source="simbad_metadata",
            ))

    hip = MeasurementValue(
        band="HP",
        value=8.36,
        resolution_major_arcsec=0.1,
        resolution_minor_arcsec=0.1,
        resolution_kind="test",
        resolution_reference="test",
    )
    paunzen = MeasurementValue(
        band="BS_YS",
        value=0.46,
        resolution_major_arcsec=0.8,
        resolution_minor_arcsec=0.8,
        resolution_kind="catalog_spatial_resolution_limit",
        resolution_reference="test",
    )
    rows = {
        "hip2": CatalogCandidate(
            source_id="36948",
            ra_deg=10.0 - 0.0003,
            dec_deg=-20.0,
            epoch=1991.25,
            payload={"HIP": 36948},
            measurements=(hip,),
        ),
        "paunzen15": CatalogCandidate(
            source_id="7109|TYC2=2638|TYC3=1",
            ra_deg=10.0 - 0.0003,
            dec_deg=-20.0,
            epoch=2000.0,
            payload={"TYC1": 7109, "TYC2": 2638, "TYC3": 1},
            measurements=(paunzen,),
        ),
    }
    for provider, row in rows.items():
        CatalogAcquisitionService(session_factory, {provider: FakeCatalog(
            [row],
            name=provider,
            release=f"fake-{provider}",
            query_epoch=row.epoch,
        )}).refresh(system.sdbid, provider)

    proposals = {
        value["provider"]: value
        for value in measurement_assignment_proposals(
            session_factory, system.sdbid
        )
        if value["provider"] in rows
    }

    assert set(proposals) == {"hip2", "paunzen15"}
    for proposal in proposals.values():
        assert proposal["proposal_confidence"] == "high"
        assert proposal["proposed_assignments"] == [{
            "target_id": component_a.target_id,
            "sdbid": component_a.sdbid,
            "role": "contributor",
            "evidence": "simbad_identifier",
            "identifier_match": True,
            "identifier_preferred": True,
            "identifier_sources": ["simbad_metadata"],
            "separation_arcsec": proposal["proposed_assignments"][0][
                "separation_arcsec"
            ],
        }]
    matrix = build_measurement_assignment_review(
        session_factory, system.sdbid
    ).matrix
    display_ids = {
        row["provider"]: row["source_display_name"] for row in matrix["rows"]
        if row["provider"] in rows
    }
    assert display_ids == {
        "hip2": "HIP 36948",
        "paunzen15": "TYC 7109-2638-1",
    }


def test_correct_source_association_makes_photometry_current_without_assignment_rows(
    session_factory,
):
    system, component_a, _component_b = _configured_system(session_factory)
    source_id = "00400007-2000000"
    simbad_id = f"2MASS J{source_id}"
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=component_a.target_id,
            value=simbad_id,
            normalized_value=normalize_identifier(simbad_id),
            source="simbad",
        ))
    resolved = MeasurementValue(
        band="2MJ", value=7.1, error=0.02, systematic_error=0.01,
        unit="mag", bibcode="test", resolution_major_arcsec=0.1,
        resolution_minor_arcsec=0.1, resolution_kind="test",
        resolution_reference="test",
    )
    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([
        candidate(source_id, measurements=[resolved])
    ])}).refresh(system.sdbid, "2mass")
    _move_catalog_association(
        session_factory,
        source=system,
        destination=component_a,
    )

    preview = apply_measurement_assignment_proposals(
        session_factory, target_reference=system.sdbid,
    )

    assert preview["mode"] == "dry_run"
    assert preview["summary"]["already_current_measurements"] == 1
    assert preview["summary"]["already_current_assignments"] == 1
    with session_factory() as session:
        assert session.query(MeasurementTargetAssociation).count() == 0
        assert session.query(MeasurementAssociationAction).count() == 0

    samples = SampleService(session_factory)
    samples.create("proposal-test")
    samples.add(
        "proposal-test", system.sdbid, actor="test", reason="system member"
    )
    samples.add(
        "proposal-test", component_a.sdbid, actor="test", reason="component member"
    )
    sample_preview = apply_measurement_assignment_proposals(
        session_factory, sample="proposal-test",
    )
    assert sample_preview["targets_evaluated"] == 2
    assert sample_preview["measurements_evaluated"] == 1
    assert sample_preview["summary"]["already_current_assignments"] == 1

    applied = apply_measurement_assignment_proposals(
        session_factory,
        target_reference=system.sdbid,
        apply=True,
        actor="reviewer",
        reason="accept tested proposal",
    )

    assert applied["summary"]["already_current_measurements"] == 1
    assert applied["summary"]["already_current_assignments"] == 1
    with session_factory() as session:
        assert session.query(MeasurementTargetAssociation).count() == 0
        assert session.query(MeasurementAssociationAction).count() == 0

    repeated = apply_measurement_assignment_proposals(
        session_factory,
        target_reference=system.sdbid,
        apply=True,
        actor="reviewer",
        reason="repeat",
    )

    assert repeated["summary"]["already_current_measurements"] == 1
    assert repeated["summary"]["already_current_assignments"] == 1
    with session_factory() as session:
        assert session.query(MeasurementTargetAssociation).count() == 0
        assert session.query(MeasurementAssociationAction).count() == 0


def test_proposal_application_does_not_replace_conflicting_current_assignment(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    source_id = "00400007-2000000"
    simbad_id = f"2MASS J{source_id}"
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=component_a.target_id,
            value=simbad_id,
            normalized_value=normalize_identifier(simbad_id),
            source="simbad",
        ))
    resolved = MeasurementValue(
        band="2MJ", value=7.1, error=0.02, systematic_error=0.01,
        unit="mag", bibcode="test", resolution_major_arcsec=0.1,
        resolution_minor_arcsec=0.1, resolution_kind="test",
        resolution_reference="test",
    )
    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([
        candidate(source_id, measurements=[resolved])
    ])}).refresh(system.sdbid, "2mass")
    measurement_id = measurement_assignment_proposals(
        session_factory, system.sdbid
    )[0]["measurement_id"]
    assign_measurement_target(
        session_factory,
        measurement_id,
        component_b.sdbid,
        actor="reviewer",
        reason="deliberate conflicting manual assignment",
    )

    result = apply_measurement_assignment_proposals(
        session_factory,
        target_reference=system.sdbid,
        apply=True,
        actor="automatic",
    )

    assert result["summary"]["skipped_conflicting_current_assignments"] == 1
    assert result["items"][0]["skip_reason"] == "conflicting_current_assignments"
    with session_factory() as session:
        associations = session.query(MeasurementTargetAssociation).all()
        assert [(row.target_id, row.role) for row in associations] == [
            (component_b.target_id, "contributor")
        ]
        assert session.query(MeasurementAssociationAction).count() == 1


def test_derived_assignment_preserves_excluded_measurement_until_overridden(
    session_factory, tmp_path,
):
    system, component_a, _component_b = _configured_system(session_factory)
    source_id = "00400007-2000000"
    simbad_id = f"2MASS J{source_id}"
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=component_a.target_id,
            value=simbad_id,
            normalized_value=normalize_identifier(simbad_id),
            source="simbad",
        ))
    excluded = MeasurementValue(
        band="2MJ", value=7.1, error=0.02, systematic_error=0.01,
        unit="mag", bibcode="test", resolution_major_arcsec=0.1,
        resolution_minor_arcsec=0.1, resolution_kind="test",
        resolution_reference="test", excluded=True,
        exclusion_reason="provider quality flag",
    )
    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([
        candidate(source_id, measurements=[excluded])
    ])}).refresh(system.sdbid, "2mass")
    _move_catalog_association(
        session_factory,
        source=system,
        destination=component_a,
    )

    result = apply_measurement_assignment_proposals(
        session_factory,
        target_reference=system.sdbid,
        apply=True,
        actor="reviewer",
    )

    assert result["summary"]["already_current_assignments"] == 1
    assert result["items"][0]["measurement_excluded"] is True
    with session_factory() as session:
        assert session.query(NormalizedMeasurement).one().excluded is True
        assert session.query(MeasurementTargetAssociation).count() == 0

    before = Table.read(
        export_ipac(session_factory, component_a.sdbid, tmp_path / "before.txt"),
        format="ascii.ipac",
    )
    assert list(before["exclude"]) == [1]

    with session_factory() as session:
        measurement_id = session.scalar(select(NormalizedMeasurement.id))
    set_measurement_eligibility(
        session_factory,
        measurement_id,
        excluded=False,
        actor="reviewer",
        reason="visual inspection accepts provider-flagged measurement",
    )
    after = Table.read(
        export_ipac(session_factory, component_a.sdbid, tmp_path / "after.txt"),
        format="ascii.ipac",
    )
    assert list(after["exclude"]) == [0]
    assert "Override:visual inspection accepts" in str(after["Note2"][0])


def test_simbad_identifier_outranks_provider_derived_duplicate_on_composite(
    session_factory,
):
    system, component_a, _component_b = _configured_system(session_factory)
    source_id = "4706564427272810624"
    gaia_id = f"Gaia DR3 {source_id}"
    with session_factory.begin() as session:
        session.add_all([
            ExternalIdentifier(
                target_id=system.target_id,
                value=gaia_id,
                normalized_value=normalize_identifier(gaia_id),
                source="gaia_dr3",
            ),
            ExternalIdentifier(
                target_id=component_a.target_id,
                value=gaia_id,
                normalized_value=normalize_identifier(gaia_id),
                source="simbad",
            ),
        ])
    resolved = MeasurementValue(
        band="GAIA.G", value=8.9, error=0.001, systematic_error=0.003,
        unit="mag", bibcode="test", resolution_major_arcsec=0.12,
        resolution_minor_arcsec=0.12, resolution_kind="test",
        resolution_reference="test",
    )
    service = CatalogAcquisitionService(session_factory, {"gaia_dr3": FakeCatalog([
        candidate(
            source_id,
            ra=10.0 - 0.0003,
            dec=-20.0,
            measurements=[resolved],
        )
    ], name="gaia_dr3", release="test", query_epoch=2016.0)})
    service.refresh(system.sdbid, "gaia_dr3")
    service.refresh(component_a.sdbid, "gaia_dr3")

    proposal = measurement_assignment_proposals(session_factory, system.sdbid)[0]

    assert proposal["proposed_assignments"] == [{
        "target_id": component_a.target_id,
        "sdbid": component_a.sdbid,
        "role": "contributor",
        "evidence": "simbad_identifier",
        "identifier_match": True,
        "identifier_preferred": True,
        "identifier_sources": ["simbad"],
        "separation_arcsec": proposal["proposed_assignments"][0]["separation_arcsec"],
    }]
    candidates = {
        row["sdbid"]: row for row in proposal["candidate_targets"]
    }
    assert candidates[component_a.sdbid]["identifier_preferred"] is True
    assert candidates[component_a.sdbid]["identifier_sources"] == ["simbad"]
    assert candidates[system.sdbid]["identifier_match"] is True
    assert candidates[system.sdbid]["identifier_preferred"] is False
    assert candidates[system.sdbid]["identifier_sources"] == ["gaia_dr3"]
    assert "simbad" in proposal["proposal_reason"]


def test_exact_simbad_composite_identifier_is_secure_without_imported_contributors(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    for target in (component_a, component_b):
        set_target_lifecycle(
            session_factory,
            target.sdbid,
            role="physical",
            state="archived",
            actor="test",
            reason="exercise a system whose physical components are not active/imported",
        )
    gaia_source = "4706564427272810624"
    twomass_source = "00400000-2000000"
    with session_factory.begin() as session:
        session.add_all([
            ExternalIdentifier(
                target_id=system.target_id,
                value=f"Gaia DR3 {gaia_source}",
                normalized_value=normalize_identifier(f"Gaia DR3 {gaia_source}"),
                source="simbad",
            ),
            ExternalIdentifier(
                target_id=system.target_id,
                value=f"2MASS J{twomass_source}",
                normalized_value=normalize_identifier(f"2MASS J{twomass_source}"),
                source="simbad",
            ),
        ])
    gaia_measurement = MeasurementValue(
        band="GAIA.G", value=8.9, error=0.001, systematic_error=0.003,
        unit="mag", bibcode="test", resolution_major_arcsec=0.12,
        resolution_minor_arcsec=0.12, resolution_kind="test",
        resolution_reference="test",
    )
    twomass_measurement = MeasurementValue(
        band="2MJ", value=7.1, error=0.02, systematic_error=0.01,
        unit="mag", bibcode="test", resolution_major_arcsec=2.5,
        resolution_minor_arcsec=2.5, resolution_kind="test",
        resolution_reference="test",
    )
    CatalogAcquisitionService(session_factory, {"gaia_dr3": FakeCatalog([
        candidate(gaia_source, measurements=[gaia_measurement])
    ], name="gaia_dr3", release="test", query_epoch=2016.0)}).refresh(
        system.sdbid, "gaia_dr3"
    )
    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([
        candidate(twomass_source, measurements=[twomass_measurement])
    ], name="2mass", release="test", query_epoch=1999.3)}).refresh(
        system.sdbid, "2mass"
    )

    proposals = {
        row["provider"]: row
        for row in measurement_assignment_proposals(session_factory, system.sdbid)
    }

    for provider in ("gaia_dr3", "2mass"):
        proposal = proposals[provider]
        assert proposal["proposal_confidence"] == "high"
        assert proposal["comparison_to_current"] == "partial_proposal"
        assert [(row["sdbid"], row["role"], row["evidence"])
                for row in proposal["proposed_assignments"]] == [
            (system.sdbid, "composite_scope", "simbad_identifier")
        ]
        assert "exact SIMBAD" in proposal["proposal_reason"]
        assert "physical component" in proposal["proposal_reason"]


def test_cli_prints_read_only_measurement_assignment_proposals(tmp_path, capsys):
    database = tmp_path / "sdb.sqlite"
    init_database(database)
    sessions = make_session_factory(database)
    system, component_a, component_b = _configured_system(sessions)
    CatalogAcquisitionService(sessions, {"allwise": _wise_catalog()}).refresh(system.sdbid, "allwise")

    assert main([
        "--database", str(database), "photometry", "proposals", system.sdbid,
    ]) == 0
    output = capsys.readouterr().out
    assert '"providers"' in output
    assert '"confidence"' in output
    assert '"review_required_measurements"' in output
    assert '"proposed_assignments"' not in output
    assert list_measurement_target_assignments(sessions, component_a.sdbid) == []

    assert main([
        "--database", str(database), "photometry", "proposals", system.sdbid,
        "--details",
    ]) == 0
    detailed = capsys.readouterr().out
    assert '"proposed_assignments"' in detailed
    assert component_a.sdbid in detailed
    assert component_b.sdbid in detailed

    assert main([
        "--database", str(database), "photometry", "apply-proposals", system.sdbid,
    ]) == 0
    output = capsys.readouterr().out
    assert '"mode": "dry_run"' in output
    assert '"skipped_not_high_confidence": 1' in output
    assert '"items"' not in output
    assert list_measurement_target_assignments(sessions, component_a.sdbid) == []


def test_proposal_compares_catalog_position_at_native_epoch(session_factory):
    native = astrometry(
        10.0, -20.0, epoch=2000.0, pmra=3000.0, pmdec=-1000.0,
        source="simbad",
    )
    target = IdentityService(
        session_factory,
        simbad=FakeSimbad({"Fast B": simbad_result("Fast B", native)}),
    ).add(AddRequest(name="Fast B"))
    catalog_position = propagate_to_epoch(native, 1999.3)
    resolved = MeasurementValue(
        band="2MJ", value=7.1, error=0.02, systematic_error=0.01,
        unit="mag", bibcode="test", resolution_major_arcsec=0.1,
        resolution_minor_arcsec=0.1, resolution_kind="test",
        resolution_reference="test",
    )
    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([
        candidate(
            "not-an-identifier",
            ra=catalog_position.ra_deg,
            dec=catalog_position.dec_deg,
            measurements=[resolved],
        )
    ])}).refresh(target.sdbid, "2mass")

    proposal = measurement_assignment_proposals(session_factory, target.sdbid)[0]

    assert proposal["proposed_assignments"][0]["sdbid"] == target.sdbid
    assert proposal["proposed_assignments"][0]["separation_arcsec"] < 0.001
    assert proposal["candidate_targets"][0]["comparison_epoch"] == 1999.3


def test_ubv_d_proposes_unique_simple_binary_without_resolution(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    HierarchyService(session_factory).add_member(
        "proposal AB", system.sdbid, component_label="AB",
    )
    with session_factory.begin() as session:
        session.add_all([
            ExternalIdentifier(
                target_id=system.target_id,
                value="HD 123",
                normalized_value=normalize_identifier("HD 123"),
                source="simbad",
            ),
            ExternalIdentifier(
                target_id=component_a.target_id,
                value="HD 123A",
                normalized_value=normalize_identifier("HD 123A"),
                source="simbad",
            ),
            ExternalIdentifier(
                target_id=component_b.target_id,
                value="HD 123B",
                normalized_value=normalize_identifier("HD 123B"),
                source="simbad",
            ),
        ])
    value = MeasurementValue(
        band="VJ",
        value=6.1,
        error=0.01,
        unit="mag",
        bibcode="2006yCat.2168....0M",
        ownership_scope="system",
        blend_state="blended",
        blend_reason="catalog_multiple_in_aperture",
    )
    catalog = FakeCatalog(
        [CatalogCandidate(
            source_id="+100000123|m_LID=D",
            ra_deg=10.0,
            dec_deg=-20.0,
            epoch=2000.0,
            payload={
                "SimbadName": "HD 123",
                "m_LID": "D",
                "_RA": 10.0,
                "_DE": -20.0,
            },
            measurements=(value,),
        )],
        name="ubvmeans",
        release="II/168",
        query_epoch=2000.0,
    )
    CatalogAcquisitionService(session_factory, {"ubvmeans": catalog}).refresh(
        system.sdbid, "ubvmeans",
    )

    proposal = measurement_assignment_proposals(
        session_factory, system.sdbid,
    )[0]

    assert proposal["catalog_component"]["kind"] == "combined_components"
    assert proposal["proposal_confidence"] == "high"
    assert {
        (row["sdbid"], row["role"])
        for row in proposal["proposed_assignments"]
    } == {
        (system.sdbid, "composite_scope"),
        (component_a.sdbid, "contributor"),
        (component_b.sdbid, "contributor"),
    }
    assert "unique simple A+B system" in proposal["proposal_reason"]


def test_ubv_d_complex_system_remains_scope_only(session_factory):
    system, component_a, component_b = _configured_system(session_factory)
    hierarchy = HierarchyService(session_factory)
    hierarchy.add_member("proposal AB", system.sdbid, component_label="AB")
    component_c = IdentityService(session_factory).add(
        AddRequest(ra_deg=10.0008, dec_deg=-20.0)
    )
    hierarchy.add_member("proposal AB", component_c.sdbid, component_label="C")
    set_target_lifecycle(
        session_factory,
        component_c.sdbid,
        role="physical",
        state="active",
        actor="test",
        reason="physical component C",
    )
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=system.target_id,
            value="HD 123",
            normalized_value=normalize_identifier("HD 123"),
            source="simbad",
        ))
    value = MeasurementValue(
        band="VJ",
        value=6.1,
        error=0.01,
        unit="mag",
        bibcode="2006yCat.2168....0M",
        ownership_scope="system",
        blend_state="blended",
        blend_reason="catalog_multiple_in_aperture",
    )
    CatalogAcquisitionService(session_factory, {
        "ubvmeans": FakeCatalog(
            [CatalogCandidate(
                source_id="+100000123|m_LID=D",
                ra_deg=10.0,
                dec_deg=-20.0,
                epoch=2000.0,
                payload={
                    "SimbadName": "HD 123",
                    "m_LID": "D",
                    "_RA": 10.0,
                    "_DE": -20.0,
                },
                measurements=(value,),
            )],
            name="ubvmeans",
            release="II/168",
            query_epoch=2000.0,
        ),
    }).refresh(system.sdbid, "ubvmeans")

    proposal = measurement_assignment_proposals(
        session_factory, system.sdbid,
    )[0]

    assert proposal["comparison_to_current"] == "partial_proposal"
    assert [
        row["role"] for row in proposal["proposed_assignments"]
    ] == ["composite_scope"]
    assert proposal["proposal_confidence"] == "low"


def test_ubv_numeric_component_maps_to_explicit_system_member(
    session_factory,
):
    system, _component_a, component_b = _configured_system(session_factory)
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=component_b.target_id,
            value="HD 123B",
            normalized_value=normalize_identifier("HD 123B"),
            source="simbad",
        ))
    value = MeasurementValue(
        band="VJ",
        value=7.2,
        error=0.01,
        unit="mag",
        bibcode="2006yCat.2168....0M",
    )
    CatalogAcquisitionService(session_factory, {
        "ubvmeans": FakeCatalog(
            [CatalogCandidate(
                source_id="+100000123|m_LID=2",
                ra_deg=10.0003,
                dec_deg=-20.0,
                epoch=2000.0,
                payload={
                    "SimbadName": "HD 123",
                    "m_LID": "2",
                    "_RA": 10.0003,
                    "_DE": -20.0,
                },
                measurements=(value,),
            )],
            name="ubvmeans",
            release="II/168",
            query_epoch=2000.0,
        ),
    }).refresh(system.sdbid, "ubvmeans")

    proposal = measurement_assignment_proposals(
        session_factory, system.sdbid,
    )[0]

    assert proposal["catalog_component"]["component_label"] == "B"
    assert proposal["proposed_assignments"] == [{
        "target_id": component_b.target_id,
        "sdbid": component_b.sdbid,
        "role": "contributor",
        "evidence": "catalog_component_code+system_membership+simbad_identifier",
        "identifier_match": True,
        "identifier_preferred": True,
        "identifier_sources": ["simbad"],
        "separation_arcsec": proposal["proposed_assignments"][0][
            "separation_arcsec"
        ],
    }]


def test_tdsc_named_component_maps_to_explicit_system_member(
    session_factory,
):
    system, component_a, _component_b = _configured_system(session_factory)
    with session_factory.begin() as session:
        session.add_all([
            ExternalIdentifier(
                target_id=system.target_id,
                value="HD 224953",
                normalized_value=normalize_identifier("HD 224953"),
                source="simbad",
            ),
            ExternalIdentifier(
                target_id=component_a.target_id,
                value="HD 224953A",
                normalized_value=normalize_identifier("HD 224953A"),
                source="simbad",
            ),
        ])
    value = MeasurementValue(
        band="VT",
        value=9.71,
        error=0.02,
        unit="mag",
        bibcode="2002A&A...384..180F",
    )
    CatalogAcquisitionService(session_factory, {
        "tdsc": FakeCatalog(
            [CatalogCandidate(
                source_id="88|m_TDSC=A",
                ra_deg=10.0 - 0.0003,
                dec_deg=-20.0,
                epoch=2000.0,
                payload={
                    "TDSC": 88,
                    "m_TDSC": "A",
                    "HD": 224953,
                    "HIP": 169,
                    "WDS": "00021-6817",
                    "TYC1": 9134,
                    "TYC2": 1714,
                    "TYC3": 1,
                },
                measurements=(value,),
            )],
            name="tdsc",
            release="I/276",
            query_epoch=2000.0,
        ),
    }).refresh(system.sdbid, "tdsc")

    proposal = measurement_assignment_proposals(
        session_factory, system.sdbid,
    )[0]

    assert proposal["source_display_name"] == "HD 224953A"
    assert proposal["catalog_component"] == {
        "native_code": "A",
        "kind": "named_component",
        "component_label": "A",
    }
    assert [
        (row["sdbid"], row["role"])
        for row in proposal["proposed_assignments"]
    ] == [(component_a.sdbid, "contributor")]
