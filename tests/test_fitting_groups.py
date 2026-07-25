from __future__ import annotations

import json

from sqlalchemy import select

from sdb_identity.catalogs import CatalogService, MeasurementValue
from sdb_identity.assignment_readiness import assignment_readiness_report
from sdb_identity.cli import main
from sdb_identity.fitting_groups import fitting_group_report
from sdb_identity.joint_fit_manifest import write_joint_fit_manifest
from sdb_identity.models import NormalizedMeasurement
from sdb_identity.photometry import assign_measurement_target, set_photometry_override
from sdb_identity.samples import SampleService
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
    CatalogService(session_factory, {"allwise": FakeCatalog(
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


def test_excluded_shared_measurement_is_context_until_manually_included(session_factory):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system, excluded=True)
    _assign_pair(session_factory, measurement, system, component_a, component_b)

    excluded = fitting_group_report(
        session_factory, target_reference=system.sdbid,
    )
    row = excluded["measurements"][0]
    assert excluded["summary"]["fitting_group_count"] == 2
    assert row["fit_enabled"] is False
    assert row["exclusion_basis"] == "provider_excluded"
    assert len(row["fitting_group_ids"]) == 2
    assert all(
        group["context_measurement_ids"] == [measurement.id]
        for group in excluded["groups"]
    )

    set_photometry_override(
        session_factory, system.sdbid, provider="allwise", band="WISE22",
        excluded=False, actor="test", reason="reviewed as usable",
    )
    included = fitting_group_report(
        session_factory, target_reference=component_b.sdbid,
    )
    assert included["summary"]["fitting_group_count"] == 1
    assert included["measurements"][0]["exclusion_basis"] == "manual_include_override"
    assert included["measurements"][0]["fit_enabled"] is True


def test_composite_only_measurement_is_reported_unresolved(session_factory):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    assign_measurement_target(
        session_factory, measurement.id, system.sdbid,
        role="composite_scope", method="test", actor="test",
        reason="scope known but contributors unresolved",
    )

    report = fitting_group_report(session_factory, target_reference=system.sdbid)

    assert report["summary"]["fitting_group_count"] == 2
    assert report["summary"]["unresolved_composite_measurement_count"] == 1
    assert report["measurements"][0]["fit_enabled"] is False
    assert report["measurements"][0]["review_flags"] == [
        "composite_scope_without_physical_contributor"
    ]
    assert {row["sdbid"] for row in report["targets"] if row["model_target"]} == {
        component_a.sdbid, component_b.sdbid,
    }


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


def test_joint_fit_manifest_preserves_assignments_and_single_export_sidecar(
    session_factory, db_path, tmp_path, capsys,
):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    _assign_pair(session_factory, measurement, system, component_a, component_b)
    manifest_path = tmp_path / "explicit-joint-fit.json"

    write_joint_fit_manifest(
        session_factory, manifest_path, target_reference=system.sdbid,
    )
    manifest = json.loads(manifest_path.read_text())
    row = manifest["graph"]["measurements"][0]
    assert manifest["schema_version"] == 1
    assert [(value["sdbid"], value["role"]) for value in row["assignments"]] == [
        (system.sdbid, "composite_scope"),
        (component_a.sdbid, "contributor"),
        (component_b.sdbid, "contributor"),
    ]
    assert row["resolution_major_arcsec"] == 12.0

    rawphot = tmp_path / "one-rawphot.txt"
    assert main([
        "--database", str(db_path), "export", system.sdbid,
        "--output", str(rawphot),
    ]) == 0
    capsys.readouterr()
    sidecar = json.loads((tmp_path / "one-joint-fit.json").read_text())
    assert sidecar["legacy_exports"][0]["sdbid"] == system.sdbid
    assert sidecar["legacy_exports"][0]["output"] == str(rawphot.resolve())
