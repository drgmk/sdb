from __future__ import annotations

import json

from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.cli import main
from sdb_identity.database import make_session_factory
from sdb_identity.export import export_ipac
from sdb_identity.metadata import MetadataQueryResult, MetadataService
from sdb_identity.models.curated import CuratedRecord, DatasetRevision
from sdb_identity.models.identity import ExternalIdentifier
from sdb_identity.readiness import ReadinessService
from sdb_identity.samples import SampleService
from sdb_identity.identifiers import normalize_identifier
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement
from tests.test_metadata import FakeMetadataProvider


def _sample_target(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    samples = SampleService(session_factory)
    samples.create("science")
    samples.add("science", target.target_id, actor="grant", reason="selected")
    return target


def test_readiness_blocks_missing_expected_provider_results(session_factory):
    target = _sample_target(session_factory)
    report = ReadinessService(session_factory).report(
        "science", providers=("simbad", "2mass"),
    )

    assert report.status == "blocked"
    assert report.blocker_count == 2
    assert {
        (issue["sdbid"], issue.get("provider"), issue["kind"])
        for issue in report.issues if issue["severity"] == "blocker"
    } == {
        (target.sdbid, "simbad", "missing_provider"),
        (target.sdbid, "2mass", "missing_provider"),
    }


def test_no_match_is_complete_and_pending_export_is_review_only(
    session_factory, tmp_path,
):
    target = _sample_target(session_factory)
    MetadataService(
        session_factory, FakeMetadataProvider(MetadataQueryResult("no_match")),
    ).refresh(target.target_id)
    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([])}).refresh(
        target.target_id, "2mass",
    )

    service = ReadinessService(session_factory)
    report = service.report("science", providers=("simbad", "2mass"))
    assert (report.status, report.blocker_count, report.pending_export_count) == (
        "review", 0, 1,
    )

    export_ipac(session_factory, target.target_id, tmp_path / "target.txt")
    ready = service.report("science", providers=("simbad", "2mass"))
    assert (ready.status, ready.blocker_count, ready.warning_count) == (
        "ready", 0, 0,
    )


def test_readiness_reports_photometry_review_signals(session_factory):
    target = _sample_target(session_factory)
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([candidate(measurements=[
            measurement(excluded=True),
        ])]),
    }).refresh(target.target_id, "2mass")

    report = ReadinessService(session_factory).report(
        "science", providers=("2mass",),
    )
    kinds = {issue["kind"] for issue in report.issues}
    assert report.status == "review"
    assert "excluded_photometry" in kinds
    assert "pending_export" in kinds


def test_readiness_blocks_only_sample_relevant_unresolved_curated_rows(
    session_factory,
):
    target = _sample_target(session_factory)
    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([])}).refresh(
        target.target_id, "2mass",
    )
    with session_factory() as session, session.begin():
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="HD 123",
            normalized_value=normalize_identifier("HD 123"),
            source="submitted",
        ))
        revision = DatasetRevision(
            dataset="test_curated",
            source_path="/test/curated.ipac",
            source_sha256="a" * 64,
            status="active",
            is_current=True,
            row_count=2,
            unresolved_count=2,
        )
        session.add(revision)
        session.flush()
        session.add_all([
            CuratedRecord(
                revision_id=revision.id,
                record_no=1,
                row_sha256="b" * 64,
                source_identifier="HD 123",
                payload_json="{}",
                association_status="unresolved",
            ),
            CuratedRecord(
                revision_id=revision.id,
                record_no=2,
                row_sha256="c" * 64,
                source_identifier="unrelated target",
                payload_json="{}",
                association_status="unresolved",
            ),
        ])

    report = ReadinessService(session_factory).report(
        "science", providers=("2mass",),
    )
    curated = [
        issue for issue in report.issues
        if issue["kind"] == "curated_record"
    ]

    assert report.sample_unresolved_curated_count == 1
    assert report.global_unresolved_curated_count == 2
    assert report.blocker_count == 1
    assert len(curated) == 1
    assert curated[0]["record_no"] == 1
    assert curated[0]["sdbid"] == target.sdbid


def test_readiness_cli_returns_nonzero_only_for_blockers(db_path, capsys):
    sessions = make_session_factory(db_path)
    _sample_target(sessions)

    assert main([
        "--database", str(db_path), "sample", "readiness", "science",
        "--providers", "2mass",
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
