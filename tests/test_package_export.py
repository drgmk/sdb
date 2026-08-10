from __future__ import annotations

import json

from astropy.table import Table
from sqlalchemy import select, text

from sdb_identity.catalogs.acquisition import CatalogAcquisitionService
from sdb_identity.catalogs.types import MeasurementValue
from sdb_identity.cli import main
from sdb_identity.models.catalogs import NormalizedMeasurement
from sdb_identity.models.exports import ExportItem, ExportRun
from sdb_identity.joint_fit import read_joint_fit
from sdb_identity.package_export import PackageExportService
from sdb_identity.photometry.assignments import (
    assign_measurement_target,
    set_measurement_eligibility,
)
from sdb_identity.samples.service import SampleService
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.target_lifecycle import set_target_lifecycle
from tests.test_catalog import FakeCatalog, candidate
from tests.test_fitting_groups import _assign_pair, _measurement
from tests.test_system_photometry_foundation import _configured_system


def _sample_with_photometry(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    values = (
        MeasurementValue("2MJ", 7.1, 0.02, unit="mag"),
        MeasurementValue("2MH", 6.9, 0.03, unit="mag", excluded=True),
        MeasurementValue(
            "2MKS", 6.8, 0.04, unit="mag", upper_limit=True, private=True,
        ),
    )
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([candidate(measurements=values)]),
    }).refresh(target.target_id, "2mass")
    samples = SampleService(session_factory)
    samples.create("science", sample_date="2026-07-06", note="Acceptance sample")
    samples.add("science", target.target_id, actor="grant", reason="selected")
    return target


def test_package_export_writes_manifest_and_sdf_readable_file(
    session_factory, tmp_path,
):
    target = _sample_with_photometry(session_factory)
    result = PackageExportService(session_factory).export(
        tmp_path, sample="science",
    )

    assert (
        result.selected_target_count,
        result.target_count,
        result.package_count,
        result.exported,
        result.failed,
    ) == (1, 1, 1, 1, 0)
    manifest = json.loads((tmp_path / f"export-{result.run_id}-manifest.json").read_text())
    assert manifest["schema"] == "sdb-fit-package-export"
    assert manifest["selection"]["sample"] == "science"
    assert manifest["database_revision"] == "0003_unified_exports"
    assert manifest["items"][0]["sdbid"] == target.sdbid
    assert manifest["items"][0]["output"] == (
        f"{target.sdbid}/{target.sdbid}-rawphot.txt"
    )
    assert len(manifest["items"][0]["sha256"]) == 64
    assert manifest["started_at"] and manifest["completed_at"]
    assert manifest["package_count"] == 1
    assert manifest["packages"][0]["directory"] == target.sdbid
    assert manifest["packages"][0]["joint_fit"] is None
    assert manifest["packages"][0]["model_sdbids"] == [target.sdbid]
    assert manifest["packages"][0]["observation_sdbids"] == [target.sdbid]
    assert not (tmp_path / target.sdbid / "joint-fit.yml").exists()

    output = tmp_path / target.sdbid / f"{target.sdbid}-rawphot.txt"
    table = Table.read(output, format="ascii.ipac")
    assert list(table["Lim"]) == [0, 0, 1]
    assert list(table["exclude"]) == [0, 1, 0]
    assert list(table["private"]) == [0, 0, 1]
    from sdf.photometry import Photometry

    photometry = Photometry.read_sdb_file(output)
    assert list(photometry.filters) == ["2MJ", "2MH", "2MKS"]
    assert list(photometry.ignore) == [False, True, False]

    with session_factory() as session:
        run = session.get(ExportRun, result.run_id)
        item = session.scalar(select(ExportItem))
        summary = session.execute(text(
            "SELECT target_count, exported_count, failed_count "
            "FROM export_summary WHERE run_id=:run_id"
        ), {"run_id": result.run_id}).one()
    assert run.status == "completed"
    assert item.status == "exported"
    assert tuple(summary) == (1, 1, 0)


def test_package_export_incremental_rerun_skips_verified_output(
    session_factory, tmp_path,
):
    target = _sample_with_photometry(session_factory)
    service = PackageExportService(session_factory)
    first = service.export(tmp_path, sample="science")
    second = service.export(tmp_path, sample="science")
    assert (first.exported, second.exported, second.skipped) == (1, 0, 1)

    output = tmp_path / target.sdbid / f"{target.sdbid}-rawphot.txt"
    output.write_text(output.read_text() + "\n", encoding="utf-8")
    repaired = service.export(tmp_path, sample="science")
    assert (repaired.exported, repaired.skipped) == (1, 0)


def test_separate_sample_roots_detect_the_same_changed_projection(
    session_factory, tmp_path,
):
    target = _sample_with_photometry(session_factory)
    samples = SampleService(session_factory)
    samples.create("second-view")
    samples.add(
        "second-view", target.target_id, actor="grant", reason="another view",
    )
    service = PackageExportService(session_factory)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    service.export(first_root, sample="science")
    service.export(second_root, sample="second-view")

    with session_factory() as session:
        measurement_id = session.scalar(
            select(NormalizedMeasurement.id)
            .where(NormalizedMeasurement.target_id == target.target_id)
            .order_by(NormalizedMeasurement.id)
        )
    set_measurement_eligibility(
        session_factory,
        measurement_id,
        excluded=True,
        actor="test",
        reason="cross-root freshness fixture",
    )

    refreshed_first = service.export(first_root, sample="science")
    refreshed_second = service.export(second_root, sample="second-view")
    assert (refreshed_first.exported, refreshed_first.skipped) == (1, 0)
    assert (refreshed_second.exported, refreshed_second.skipped) == (1, 0)


def test_target_and_all_selectors_share_the_package_exporter(
    session_factory, tmp_path,
):
    first = _sample_with_photometry(session_factory)
    second = IdentityService(session_factory).add(
        AddRequest(ra_deg=40, dec_deg=20)
    )
    service = PackageExportService(session_factory)

    selected = service.export(tmp_path / "target", target_reference=first.sdbid)
    all_targets = service.export(tmp_path / "all", all_targets=True)

    assert (selected.selection_kind, selected.selected_target_count) == (
        "target", 1,
    )
    assert (all_targets.selection_kind, all_targets.selected_target_count) == (
        "all", 2,
    )
    assert (
        tmp_path / "all" / second.sdbid / f"{second.sdbid}-rawphot.txt"
    ).is_file()


def test_export_sample_selection_cli(session_factory, db_path, tmp_path, capsys):
    _sample_with_photometry(session_factory)
    assert main([
        "--database", str(db_path), "export", "--sample", "science",
        "--output-dir", str(tmp_path), "--workers", "1",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["target_count"] == 1
    assert result["exported"] == 1


def test_package_export_keeps_empty_photometry_target_in_manifest(
    session_factory, tmp_path,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1, dec_deg=2))
    samples = SampleService(session_factory)
    samples.create("empty")
    samples.add("empty", target.target_id, actor="grant", reason="selected")

    result = PackageExportService(session_factory).export(
        tmp_path, sample="empty",
    )
    output = tmp_path / target.sdbid / f"{target.sdbid}-rawphot.txt"
    assert (result.exported, result.failed) == (1, 0)
    assert len(Table.read(output, format="ascii.ipac")) == 0
    from sdf.photometry import Photometry

    assert Photometry.read_sdb_file(output) is None


def test_package_export_records_partial_failure(session_factory, tmp_path, monkeypatch):
    _sample_with_photometry(session_factory)

    def fail_export(*_args, **_kwargs):
        raise RuntimeError("synthetic export failure")

    monkeypatch.setattr("sdb_identity.package_export.write_ipac_atomic", fail_export)
    result = PackageExportService(session_factory).export(
        tmp_path, sample="science",
    )
    manifest = json.loads((tmp_path / f"export-{result.run_id}-manifest.json").read_text())

    assert (result.exported, result.failed) == (0, 1)
    assert manifest["status"] == "partial"
    assert manifest["items"][0]["error"] == "synthetic export failure"
    with session_factory() as session:
        assert session.get(ExportRun, result.run_id).status == "partial"
        assert session.scalar(select(ExportItem).where(
            ExportItem.run_id == result.run_id
        )).status == "failed"


def test_package_export_packages_joint_physical_inputs_under_composite_sdbid(
    session_factory, tmp_path,
):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _measurement(session_factory, system)
    _assign_pair(session_factory, measurement, system, component_a, component_b)
    component_measurements = []
    with session_factory.begin() as session:
        for target, band, value in (
            (component_a, "BT", 8.1),
            (component_b, "VT", 9.2),
        ):
            row = NormalizedMeasurement(
                run_id=measurement.run_id,
                target_id=target.target_id,
                raw_row_id=measurement.raw_row_id,
                detection_id=measurement.detection_id,
                measurement_key=f"fixture:{band}",
                provider=measurement.provider,
                source_id=measurement.source_id,
                band=band,
                value=value,
                error=0.03,
                systematic_error=0.01,
                unit="mag",
                bibcode="test",
            )
            session.add(row)
            session.flush()
            component_measurements.append((target, row.id))
    for target, measurement_id in component_measurements:
        assign_measurement_target(
            session_factory,
            measurement_id,
            target.sdbid,
            role="contributor",
            method="test",
            actor="test",
            reason="component-only fixture",
        )
    samples = SampleService(session_factory)
    samples.create("joint")
    samples.add("joint", system.sdbid, actor="test", reason="system sample")

    result = PackageExportService(session_factory).export(
        tmp_path, sample="joint",
    )

    assert (
        result.selected_target_count,
        result.target_count,
        result.package_count,
        result.failed,
    ) == (1, 3, 1, 0)
    package_dir = tmp_path / system.sdbid
    expected_files = {
        f"{component_a.sdbid}-rawphot.txt",
        f"{component_b.sdbid}-rawphot.txt",
        f"{system.sdbid}-rawphot.txt",
        "joint-fit.yml",
    }
    assert {path.name for path in package_dir.iterdir()} == expected_files

    definition = read_joint_fit(package_dir / "joint-fit.yml")
    assert definition.observations == {
        system.sdbid: (component_a.sdbid, component_b.sdbid),
    }
    component_a_table = Table.read(
        package_dir / f"{component_a.sdbid}-rawphot.txt",
        format="ascii.ipac",
    )
    component_b_table = Table.read(
        package_dir / f"{component_b.sdbid}-rawphot.txt",
        format="ascii.ipac",
    )
    assert list(component_a_table["Band"]) == ["BT"]
    assert list(component_b_table["Band"]) == ["VT"]
    combined = Table.read(
        package_dir / f"{system.sdbid}-rawphot.txt",
        format="ascii.ipac",
    )
    assert len(combined) == 1
    assert list(combined["Band"]) == ["WISE22"]
    assert sum(map(len, (component_a_table, component_b_table, combined))) == 3
    assert combined.meta["keywords"]["id"]["value"] == system.sdbid

    manifest = json.loads(
        (tmp_path / f"export-{result.run_id}-manifest.json").read_text()
    )
    package = manifest["packages"][0]
    assert package["primary_sdbid"] == system.sdbid
    assert package["selected_sdbids"] == [system.sdbid]
    assert package["model_sdbids"] == sorted([
        component_a.sdbid, component_b.sdbid,
    ])
    assert package["observation_sdbids"] == sorted([
        system.sdbid, component_a.sdbid, component_b.sdbid,
    ])
    assert package["joint_fit"]["path"] == (
        f"{system.sdbid}/joint-fit.yml"
    )


def test_package_export_rejects_composite_without_a_physical_fit_group(
    session_factory, tmp_path,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1, dec_deg=2))
    set_target_lifecycle(
        session_factory, target.sdbid,
        role="composite", state="system_only",
        actor="test", reason="unresolved composite fixture",
    )
    samples = SampleService(session_factory)
    samples.create("unresolved")
    samples.add("unresolved", target.sdbid, actor="test", reason="fixture")

    try:
        PackageExportService(session_factory).export(
            tmp_path, sample="unresolved",
        )
    except ValueError as error:
        assert target.sdbid in str(error)
        assert "physical fitting groups" in str(error)
    else:
        raise AssertionError("expected unresolved composite export to fail")
