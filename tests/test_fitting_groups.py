from __future__ import annotations

import json

from sqlalchemy import select

from sdb_identity.catalogs.acquisition import CatalogAcquisitionService
from sdb_identity.catalogs.types import MeasurementValue
from sdb_identity.photometry.readiness import assignment_readiness_report
from sdb_identity.cli import main
from sdb_identity.fitting_groups import fitting_group_report
from sdb_identity.joint_fit import read_joint_fit
from sdb_identity.models.photometry import MeasurementTargetAssociation
from sdb_identity.models.catalogs import NormalizedMeasurement, RawCatalogRow
from sdb_identity.photometry.assignments import (
    assign_measurement_target,
    set_measurement_eligibility,
)
from sdb_identity.review.actions import review_catalog_target_association_decision
from sdb_identity.samples.service import SampleService
from tests.test_catalog import FakeCatalog, candidate
from tests.test_system_photometry_foundation import _configured_system
from tests.test_system_expansion import _root_with_metadata


def _measurement(session_factory, system, *, excluded=False):
    value = MeasurementValue(
        band="WISE22", value=5.1, error=0.05, systematic_error=0.02,
        unit="mag", bibcode="test", excluded=excluded,
        exclusion_reason="provider quality" if excluded else None,
        resolution_major_arcsec=12.0, resolution_minor_arcsec=12.0,
        resolution_kind="fwhm", resolution_reference="test",
    )
    CatalogAcquisitionService(session_factory, {"allwise": FakeCatalog(
        [candidate("joint-wise", measurements=[value])],
        name="allwise", release="test", query_epoch=2010.5,
    )}).refresh(system.sdbid, "allwise")
    with session_factory() as session:
        return session.scalar(select(NormalizedMeasurement).where(
            NormalizedMeasurement.target_id == system.target_id,
            NormalizedMeasurement.band == "WISE22",
        ))


def _assign_pair(session_factory, measurement, system, component_a, component_b):
    for target, role in (
        (component_a, "contributor"),
        (component_b, "contributor"),
        (system, "composite_scope"),
    ):
        assign_measurement_target(
            session_factory, measurement.id, target.sdbid,
            role=role, method="test", actor="test", reason="joint measurement",
        )


def test_included_shared_measurement_connects_physical_targets(session_factory):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    _assign_pair(session_factory, measurement, system, component_a, component_b)

    report = fitting_group_report(
        session_factory, target_reference=component_a.sdbid,
    )

    assert report["invariants"]["valid"] is True
    assert report["summary"]["fitting_group_count"] == 1
    assert report["groups"][0]["sdbids"] == sorted([
        component_a.sdbid, component_b.sdbid,
    ])
    assert report["groups"][0]["fit_measurement_ids"] == [measurement.id]
    target_by_sdbid = {row["sdbid"]: row for row in report["targets"]}
    assert target_by_sdbid[system.sdbid]["model_target"] is False
    assert target_by_sdbid[component_a.sdbid]["model_target"] is True


def test_excluded_shared_measurement_keeps_package_topology(session_factory):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system, excluded=True)
    _assign_pair(session_factory, measurement, system, component_a, component_b)

    excluded = fitting_group_report(
        session_factory, target_reference=system.sdbid,
    )
    row = excluded["measurements"][0]
    assert excluded["summary"]["fitting_group_count"] == 1
    assert row["fit_enabled"] is False
    assert row["exclusion_basis"] == "provider_excluded"
    assert row["fitting_group_ids"] == [excluded["groups"][0]["group_id"]]
    assert excluded["groups"][0]["context_measurement_ids"] == [measurement.id]

    set_measurement_eligibility(
        session_factory, measurement.id,
        excluded=False, actor="test", reason="reviewed as usable",
    )
    included = fitting_group_report(
        session_factory, target_reference=component_b.sdbid,
    )
    assert included["summary"]["fitting_group_count"] == 1
    assert included["measurements"][0]["exclusion_basis"] == "manual_include_action"
    assert included["measurements"][0]["fit_enabled"] is True


def test_composite_source_association_derives_scope_without_stored_assignment(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)

    report = fitting_group_report(session_factory, target_reference=system.sdbid)

    assert report["summary"]["fitting_group_count"] == 2
    assert report["summary"]["unresolved_composite_measurement_count"] == 1
    assert report["measurements"][0]["fit_enabled"] is False
    assert report["measurements"][0]["review_flags"] == [
        "composite_scope_without_physical_contributor"
    ]
    assert report["measurements"][0]["assignments"] == [{
        "target_id": system.target_id,
        "sdbid": system.sdbid,
        "role": "composite_scope",
        "method": "catalog_association_default",
        "weight": None,
        "note": "Derived from one accepted catalog-source association",
        "association_id": None,
        "derived": True,
    }]
    assert {row["sdbid"] for row in report["targets"] if row["model_target"]} == {
        component_a.sdbid, component_b.sdbid,
    }
    with session_factory() as session:
        assert session.query(MeasurementTargetAssociation).count() == 0


def test_physical_source_association_derives_fit_contributor(session_factory):
    _system, component_a, _component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, component_a)

    report = fitting_group_report(
        session_factory,
        target_reference=component_a.sdbid,
    )

    row = next(
        value for value in report["measurements"]
        if value["measurement_id"] == measurement.id
    )
    assert row["fit_enabled"] is True
    assert row["contributor_sdbids"] == [component_a.sdbid]
    assert row["assignments"][0]["derived"] is True
    assert report["summary"]["unassigned_measurement_count"] == 0
    with session_factory() as session:
        assert session.query(MeasurementTargetAssociation).count() == 0


def test_multiple_source_associations_remain_unassigned_for_review(session_factory):
    system, component_a, _component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    with session_factory() as session:
        raw = session.query(RawCatalogRow).one()
    preview = review_catalog_target_association_decision(
        session_factory,
        target_reference=component_a.sdbid,
        detection_id=measurement.detection_id,
        action="accept",
        reviewed_raw_row_id=raw.id,
    )
    review_catalog_target_association_decision(
        session_factory,
        target_reference=component_a.sdbid,
        detection_id=measurement.detection_id,
        action="accept",
        reviewed_raw_row_id=raw.id,
        apply=True,
        actor="reviewer",
        reason="deliberately retain two plausible source associations",
        expected_token=preview["state_token"],
    )

    report = fitting_group_report(session_factory, target_reference=system.sdbid)

    row = next(
        value for value in report["measurements"]
        if value["measurement_id"] == measurement.id
    )
    assert set(row["encounter_sdbids"]) == {system.sdbid, component_a.sdbid}
    assert row["assignments"] == []
    assert row["review_flags"] == ["no_current_assignment"]


def test_unspecified_scope_requires_role_review_not_composite_resolution(
    session_factory,
):
    target = _root_with_metadata(session_factory)
    measurement = _measurement(session_factory, target)
    assign_measurement_target(
        session_factory, measurement.id, target.sdbid,
        role="composite_scope", method="test", actor="test",
        reason="scope proposed before target role review",
    )

    report = fitting_group_report(session_factory, target_reference=target.sdbid)

    assert report["summary"]["unresolved_composite_measurement_count"] == 0
    assert report["summary"]["scope_role_review_measurement_count"] == 1
    assert report["measurements"][0]["review_flags"] == [
        "scope_assignment_requires_target_role_review"
    ]
    assert report["scope_role_review_measurements"][0]["scope_sdbids"] == [
        target.sdbid
    ]


def test_assignment_readiness_groups_scope_and_previews_relatives(session_factory):
    target = _root_with_metadata(session_factory)
    measurement = _measurement(session_factory, target)
    assign_measurement_target(
        session_factory, measurement.id, target.sdbid,
        role="composite_scope", method="test", actor="test",
        reason="scope proposed before target role review",
    )

    report = assignment_readiness_report(
        session_factory, target_reference=target.sdbid,
    )
    row = report["rows"][0]

    assert report["summary"]["scope_target_count"] == 1
    assert report["summary"]["unspecified_role_target_count"] == 1
    assert report["summary"]["confirmed_composite_target_count"] == 0
    assert row["classification"] == "target_role_unspecified"
    assert row["recommended_action"] == (
        "decide whether the target is physical or composite"
    )
    assert row["measurement_count"] == 1
    assert row["detection_count"] == 1
    assert row["providers"] == [{
        "provider": "allwise",
        "measurement_count": 1,
        "included_count": 1,
        "detection_count": 1,
        "bands": ["WISE22"],
    }]
    assert row["importable_relative_count"] == 1
    assert row["context_only_relative_count"] == 2
    assert row["relative_review_required_count"] == 1


def test_assignment_readiness_finds_imported_contributors_for_composite(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    assign_measurement_target(
        session_factory, measurement.id, system.sdbid,
        role="composite_scope", method="test", actor="test",
        reason="known AB measurement scope",
    )

    report = assignment_readiness_report(
        session_factory, target_reference=system.sdbid,
    )
    row = report["rows"][0]

    assert row["classification"] == "confirmed_composite_missing_contributors"
    assert row["priority"] == "highest"
    assert row["relative_preview_error"] == (
        "target has no current SIMBAD metadata; run sdb update --providers simbad"
    )
    assert {value["sdbid"] for value in row["imported_physical_relatives"]} == {
        component_a.sdbid, component_b.sdbid,
    }
    assert row["recommended_action"] == (
        "assign imported physical relatives as contributors"
    )


def test_assignment_readiness_cli_table_and_json(
    session_factory, db_path, capsys,
):
    target = _root_with_metadata(session_factory)
    measurement = _measurement(session_factory, target)
    assign_measurement_target(
        session_factory, measurement.id, target.sdbid,
        role="composite_scope", method="test", actor="test", reason="fixture",
    )

    assert main([
        "--database", str(db_path), "photometry", "fitting-groups", "--view", "readiness",
        target.sdbid,
    ]) == 0
    table = capsys.readouterr().out
    assert target.sdbid in table
    assert "providers/bands" in table
    assert "decide whether the target is physical or composite" in table

    assert main([
        "--database", str(db_path), "photometry", "fitting-groups", "--view", "readiness",
        target.sdbid, "--format", "json",
    ]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["summary"]["scope_target_count"] == 1


def test_sample_and_cli_report_same_canonical_group(tmp_path, session_factory, db_path, capsys):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    _assign_pair(session_factory, measurement, system, component_a, component_b)
    samples = SampleService(session_factory)
    samples.create("joint")
    for target in (system, component_a, component_b):
        samples.add("joint", target.sdbid, actor="test", reason="fixture")

    report = fitting_group_report(session_factory, sample="joint")
    assert report["summary"]["measurement_count"] == 1
    assert report["summary"]["fitting_group_count"] == 1

    assert main([
        "--database", str(db_path), "photometry", "fitting-groups",
        "--sample", "joint",
    ]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["groups"] == report["groups"]
    assert cli_report["invariants"]["valid"] is True


def test_fitting_groups_requires_exactly_one_selection(session_factory):
    system, _, _ = _configured_system(session_factory)

    for kwargs in ({}, {"target_reference": system.sdbid, "sample": "x"}):
        try:
            fitting_group_report(session_factory, **kwargs)
        except ValueError as error:
            assert "exactly one" in str(error)
        else:
            raise AssertionError("expected selection validation error")


def test_joint_fit_yaml_describes_only_observation_contributors(
    session_factory, db_path, tmp_path, capsys,
):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    _assign_pair(session_factory, measurement, system, component_a, component_b)
    export_root = tmp_path / "exports"
    assert main([
        "--database", str(db_path), "export", system.sdbid,
        "--output-dir", str(export_root), "--workers", "1",
    ]) == 0
    capsys.readouterr()
    definition = read_joint_fit(
        export_root / system.sdbid / "joint-fit.yml"
    )
    assert definition.version == 1
    assert definition.observations == {
        system.sdbid: (component_a.sdbid, component_b.sdbid),
    }
