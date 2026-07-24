from __future__ import annotations

import json

from astropy.table import Table
from sqlalchemy import select, text

from sdb_identity.catalogs import CatalogService, MeasurementValue
from sdb_identity.cli import main
from sdb_identity.models import SampleExportItem, SampleExportRun
from sdb_identity.sample_export import SampleExportService
from sdb_identity.samples import SampleService
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate


def _sample_with_photometry(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    values = (
        MeasurementValue("2MJ", 7.1, 0.02, unit="mag"),
        MeasurementValue("2MH", 6.9, 0.03, unit="mag", excluded=True),
        MeasurementValue(
            "2MKS", 6.8, 0.04, unit="mag", upper_limit=True, private=True,
        ),
    )
    CatalogService(session_factory, {
        "2mass": FakeCatalog([candidate(measurements=values)]),
    }).refresh(target.target_id, "2mass")
    samples = SampleService(session_factory)
    samples.create("science", sample_date="2026-07-06", note="Acceptance sample")
    samples.add("science", target.target_id, actor="grant", reason="selected")
    return target


def test_sample_export_writes_manifest_and_sdf_readable_file(
    session_factory, tmp_path,
):
    target = _sample_with_photometry(session_factory)
    result = SampleExportService(session_factory).export("science", tmp_path)

    assert (result.target_count, result.exported, result.failed) == (1, 1, 0)
    manifest = json.loads((tmp_path / f"sample-{result.run_id}-manifest.json").read_text())
    assert manifest["database_revision"] == "0039_drop_hierarchy_edge_tables"
    assert manifest["items"][0]["sdbid"] == target.sdbid
    assert len(manifest["items"][0]["sha256"]) == 64
    assert manifest["started_at"] and manifest["completed_at"]
    joint_path = tmp_path / f"sample-{result.run_id}-joint-fit.json"
    assert result.joint_fit_manifest == str(joint_path)
    assert manifest["joint_fit"]["path"] == str(joint_path)
    assert len(manifest["joint_fit"]["sha256"]) == 64
    joint = json.loads(joint_path.read_text())
    assert joint["schema"] == "sdb-joint-fit-manifest"
    assert joint["schema_version"] == 1
    assert joint["database_revision"] == manifest["database_revision"]
    assert joint["legacy_exports"][0]["sdbid"] == target.sdbid
    assert joint["graph"]["summary"]["unassigned_measurement_count"] == 3
    assert joint["graph"]["invariants"]["valid"] is True

    output = tmp_path / f"{target.sdbid}-rawphot.txt"
    table = Table.read(output, format="ascii.ipac")
    assert list(table["Lim"]) == [0, 0, 1]
    assert list(table["exclude"]) == [0, 1, 0]
    assert list(table["private"]) == [0, 0, 1]
    from sdf.photometry import Photometry

    photometry = Photometry.read_sdb_file(output)
    assert list(photometry.filters) == ["2MJ", "2MH", "2MKS"]
    assert list(photometry.ignore) == [False, True, False]

    with session_factory() as session:
        run = session.get(SampleExportRun, result.run_id)
        item = session.scalar(select(SampleExportItem))
        summary = session.execute(text(
            "SELECT target_count, exported_count, failed_count "
            "FROM sample_export_summary WHERE run_id=:run_id"
        ), {"run_id": result.run_id}).one()
    assert run.status == "completed"
    assert item.status == "exported"
    assert tuple(summary) == (1, 1, 0)


def test_sample_export_incremental_rerun_skips_verified_output(
    session_factory, tmp_path,
):
    target = _sample_with_photometry(session_factory)
    service = SampleExportService(session_factory)
    first = service.export("science", tmp_path)
    second = service.export("science", tmp_path)
    assert (first.exported, second.exported, second.skipped) == (1, 0, 1)

    output = tmp_path / f"{target.sdbid}-rawphot.txt"
    output.write_text(output.read_text() + "\n", encoding="utf-8")
    repaired = service.export("science", tmp_path)
    assert (repaired.exported, repaired.skipped) == (1, 0)


def test_export_sample_cli(session_factory, db_path, tmp_path, capsys):
    _sample_with_photometry(session_factory)
    assert main([
        "--database", str(db_path), "export-sample", "science",
        "--output-dir", str(tmp_path),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["target_count"] == 1
    assert result["exported"] == 1


def test_sample_export_keeps_empty_photometry_target_in_manifest(
    session_factory, tmp_path,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1, dec_deg=2))
    samples = SampleService(session_factory)
    samples.create("empty")
    samples.add("empty", target.target_id, actor="grant", reason="selected")

    result = SampleExportService(session_factory).export("empty", tmp_path)
    output = tmp_path / f"{target.sdbid}-rawphot.txt"
    assert (result.exported, result.failed) == (1, 0)
    assert len(Table.read(output, format="ascii.ipac")) == 0
    from sdf.photometry import Photometry

    assert Photometry.read_sdb_file(output) is None


def test_sample_export_records_partial_failure(session_factory, tmp_path, monkeypatch):
    _sample_with_photometry(session_factory)

    def fail_export(*_args, **_kwargs):
        raise RuntimeError("synthetic export failure")

    monkeypatch.setattr("sdb_identity.sample_export.export_ipac", fail_export)
    result = SampleExportService(session_factory).export("science", tmp_path)
    manifest = json.loads((tmp_path / f"sample-{result.run_id}-manifest.json").read_text())

    assert (result.exported, result.failed) == (0, 1)
    assert manifest["status"] == "partial"
    assert manifest["items"][0]["error"] == "synthetic export failure"
    with session_factory() as session:
        assert session.get(SampleExportRun, result.run_id).status == "partial"
        assert session.scalar(select(SampleExportItem).where(
            SampleExportItem.run_id == result.run_id
        )).status == "failed"
