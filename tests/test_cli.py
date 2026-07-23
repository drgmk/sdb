from __future__ import annotations

import json
from pathlib import Path

from sdb_identity.catalogs import CatalogService
from sdb_identity.adapters.allwise import AllWiseAdapter
from sdb_identity.cli import main
from sdb_identity.database import make_session_factory
from sdb_identity.metadata import MetadataQueryResult, MetadataService
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement
from tests.test_metadata import FakeMetadataProvider, snapshot


def test_cli_init_add_and_status(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()

    assert main(["--database", str(database), "--offline", "add", "--ra", "10", "--dec", "-20"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["created"] is True

    assert main(["--database", str(database), "--offline", "status", added["sdbid"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["sdbid"] == added["sdbid"]
    assert status["hierarchy"]["classification"] == "single_or_no_known_hierarchy"
    assert status["hierarchy"]["matched_systems"] == 0

    assert main(["--database", str(database), "--offline", "hierarchy", "target", added["sdbid"]]) == 0
    hierarchy = json.loads(capsys.readouterr().out)
    assert hierarchy["target"]["sdbid"] == added["sdbid"]
    assert hierarchy["classification"] == "single_or_no_known_hierarchy"


def test_cli_hierarchy_photometry_review_lists_targets(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "add", "--ra", "10", "--dec", "-20"
    ]) == 0
    added = json.loads(capsys.readouterr().out)

    assert main([
        "--database", str(database), "hierarchy", "photometry-review", "--all"
    ]) == 0
    table = capsys.readouterr().out

    assert "sdbid" in table
    assert "class" in table
    assert "assignment" in table
    assert added["sdbid"] in table
    assert "single_or_unknown" in table

    assert main([
        "--database", str(database), "hierarchy", "photometry-review",
        "--all", "--format", "jsonl",
    ]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert rows[0]["sdbid"] == added["sdbid"]
    assert rows[0]["target_level"] == "single_or_unknown"
    assert rows[0]["measurement_count"] == 0

    assert main([
        "--database", str(database), "hierarchy", "photometry-review",
    ]) == 2
    assert "provide exactly one" in capsys.readouterr().err


def test_cli_hierarchy_review_queue_lists_targets(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "add", "--ra", "10", "--dec", "-20"
    ]) == 0
    added = json.loads(capsys.readouterr().out)

    assert main([
        "--database", str(database), "hierarchy", "review-queue", "--all"
    ]) == 0
    table = capsys.readouterr().out

    assert "priority" in table
    assert "basis" in table
    assert added["sdbid"] in table
    assert "no hierarchy review item" in table

    assert main([
        "--database", str(database), "hierarchy", "review-queue",
        "--all", "--format", "jsonl",
    ]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert rows[0]["sdbid"] == added["sdbid"]
    assert rows[0]["priority"] == "none"
    assert rows[0]["basis"] == "none"

    assert main([
        "--database", str(database), "hierarchy", "review-queue",
    ]) == 2
    assert "provide exactly one" in capsys.readouterr().err


def test_cli_unresolved_name_returns_error(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    assert main(["--database", str(database), "--offline", "add", "Unknown source"]) == 2
    assert "could not be resolved" in capsys.readouterr().err


def test_cli_catalog_status_and_export(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogService(
        sessions,
        {"2mass": FakeCatalog([candidate(measurements=[measurement()])])},
    ).refresh(target.sdbid, "2mass")

    assert main(["--database", str(database), "catalog-status", target.sdbid]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["provider"] == "2mass"
    assert status["status"] == "match"

    output = tmp_path / "rawphot.txt"
    assert main(["--database", str(database), "export", target.sdbid, "--output", str(output)]) == 0
    capsys.readouterr()
    assert output.exists()


def test_cli_rejects_offline_catalog_refresh(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "refresh", "missing", "--provider", "2mass"
    ]) == 2
    assert "unavailable in offline mode" in capsys.readouterr().err


def test_cli_metadata_status_and_notes(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    MetadataService(
        sessions,
        FakeMetadataProvider(MetadataQueryResult("match", (snapshot(),))),
    ).refresh(target.sdbid)

    assert main(["--database", str(database), "metadata-status", target.sdbid]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["provider"] == "simbad"
    assert status["status"] == "match"

    assert main([
        "--database", str(database), "note", "add", target.sdbid,
        "Check companion", "--actor", "grant",
    ]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["text"] == "Check companion"

    assert main(["--database", str(database), "note", "list", target.sdbid]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["actor"] == "grant"


def test_cli_offline_batch_import_status_and_resume(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    targets = tmp_path / "targets.csv"
    targets.write_text("ra,dec,tag\n10,-20,test\n")
    main(["--database", str(database), "init"])
    capsys.readouterr()

    assert main([
        "--database", str(database), "--offline", "import", str(targets),
        "--workers", "identity=2",
    ]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["status"] == "completed"

    assert main(["--database", str(database), "import-status", str(imported["run_id"])]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["job_counts"] == {"succeeded": 1}

    assert main([
        "--database", str(database), "--offline", "resume", str(imported["run_id"])
    ]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "completed"


def test_cli_lists_and_exports_only_dirty_targets(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    output_dir = tmp_path / "exports"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "add",
        "--ra", "10", "--dec", "-20",
    ]) == 0
    added = json.loads(capsys.readouterr().out)
    assert main(["--database", str(database), "dirty"]) == 0
    dirty = json.loads(capsys.readouterr().out)
    assert dirty["sdbid"] == added["sdbid"]
    assert main([
        "--database", str(database), "export-dirty",
        "--output-dir", str(output_dir),
    ]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1] == {"exported": 1, "failed": 0}
    assert main(["--database", str(database), "dirty"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_offline_update_reports_missing_snapshot(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    reference = tmp_path / "reference.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    main([
        "--database", str(database), "--offline", "add",
        "--ra", "10", "--dec", "-20",
    ])
    target = json.loads(capsys.readouterr().out)
    assert main([
        "--database", str(database),
        "--reference-database", str(reference),
        "--offline", "update", target["sdbid"],
        "--providers", "hip2",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["missing"] == 1
    assert result["items"][0]["action"] == "missing"


def test_cli_batch_validates_worker_settings(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    targets = tmp_path / "targets.csv"
    targets.write_text("ra,dec\n10,-20\n")
    main(["--database", str(database), "init"])
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "import", str(targets),
        "--workers", "unknown=0",
    ]) == 2
    assert "invalid worker setting" in capsys.readouterr().err


def test_cli_photometry_override_history(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    assert main([
        "--database", str(database), "photometry", "exclude", target.sdbid,
        "WISE3P4", "--provider", "allwise", "--actor", "grant",
        "--reason", "blended",
    ]) == 0
    excluded = json.loads(capsys.readouterr().out)
    assert excluded["excluded"] is True
    assert main([
        "--database", str(database), "photometry", "list", target.sdbid,
    ]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["reason"] == "blended"



def test_cli_photometry_association_decision_review(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogService(sessions, {
        "allwise": FakeCatalog([candidate(
            "wise-a", ra=10, dec=-20, measurements=[measurement("WISE3P4", 8.1)]
        )], name="allwise", release="fake-allwise"),
    }).refresh(target.sdbid, "allwise")

    assert main([
        "--database", str(database), "photometry", "review", target.sdbid,
    ]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["provider"] == "allwise"
    assert rows[0]["current_decision_scope"] is None

    assert main([
        "--database", str(database), "photometry", "set-scope", target.sdbid,
        "allwise", "wise-a", "--band", "WISE3P4", "--scope", "blended",
        "--actor", "grant", "--reason", "binary beam",
    ]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["scope"] == "blended"

    assert main([
        "--database", str(database), "photometry", "decisions", target.sdbid,
    ]) == 0
    decisions = json.loads(capsys.readouterr().out)
    assert decisions[0]["reason"] == "binary beam"


def test_cli_photometry_review_queue_lists_sample_targets(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogService(sessions, {
        "allwise": FakeCatalog([candidate(
            "wise-a", ra=10, dec=-20, measurements=[measurement("WISE3P4", 8.1)]
        )], name="allwise", release="fake-allwise"),
    }).refresh(target.sdbid, "allwise")
    assert main([
        "--database", str(database), "sample", "create", "science",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "sample", "add", "science", target.sdbid,
        "--actor", "grant", "--reason", "unit test",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "photometry", "set-scope", target.sdbid,
        "allwise", "wise-a", "--band", "WISE3P4", "--scope", "reject",
        "--actor", "grant", "--reason", "wrong component",
    ]) == 0
    capsys.readouterr()

    assert main([
        "--database", str(database), "photometry", "review-queue",
        "--sample", "science",
    ]) == 0
    table = capsys.readouterr().out
    assert "priority" in table
    assert target.sdbid in table
    assert "association rejected" in table

    assert main([
        "--database", str(database), "photometry", "review-queue",
        "--sample", "science", "--format", "jsonl",
    ]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["current_decision"] == "reject"
    assert rows[0]["action"] == "confirm export exclusion remains intended"

    assert main([
        "--database", str(database), "photometry", "review-queue",
    ]) == 2
    assert "provide exactly one" in capsys.readouterr().err


def test_cli_photometry_review_html_writes_bundle(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogService(sessions, {
        "allwise": FakeCatalog([candidate(
            "wise-a", ra=10, dec=-20, measurements=[measurement("WISE3P4", 8.1)]
        )], name="allwise", release="fake-allwise"),
    }).refresh(target.sdbid, "allwise")
    assert main([
        "--database", str(database), "sample", "create", "science",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "sample", "add", "science", target.sdbid,
        "--actor", "grant", "--reason", "unit test",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "photometry", "set-scope", target.sdbid,
        "allwise", "wise-a", "--band", "WISE3P4", "--scope", "reject",
        "--actor", "grant", "--reason", "wrong component",
    ]) == 0
    capsys.readouterr()

    output_dir = tmp_path / "reviews"
    assert main([
        "--database", str(database), "photometry", "review-html",
        "--sample", "science", "--output-dir", str(output_dir),
    ]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["targets"] == 1
    assert result["queue_rows"] >= 1
    assert result["signal_rows"] >= 1
    assert Path(result["index"]).exists()
    assert len(result["review_pages"]) == 1
    page = Path(result["review_pages"][0])
    assert page.exists()
    assert "Plotly.newPlot" in page.read_text()
    index = (output_dir / "index.html").read_text()
    assert target.sdbid in index
    assert "association rejected" in index

def test_cli_reviews_and_overrides_ambiguous_catalog_match(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    adapter = AllWiseAdapter()
    rows = [
        {"AllWISE": "one", "RAJ2000": 10.00010, "DEJ2000": -20,
         "qph": "AAAA", "ccf": "0000", "W1mag": 7.0, "e_W1mag": 0.1},
        {"AllWISE": "two", "RAJ2000": 10.00011, "DEJ2000": -20,
         "qph": "AAAA", "ccf": "0000", "W1mag": 8.0, "e_W1mag": 0.1},
    ]
    adapter.query = lambda context: [adapter.parse_row(row) for row in rows]
    CatalogService(sessions, {"allwise": adapter}).refresh(target.sdbid, "allwise")

    assert main(["--database", str(database), "review", "catalog-matches"]) == 0
    reviewed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    chosen = next(value for value in reviewed if value["source_id"] == "two")
    assert main([
        "--database", str(database), "override-catalog-match",
        str(chosen["candidate_id"]), "--actor", "grant", "--reason", "image check",
    ]) == 0
    replaced = json.loads(capsys.readouterr().out)
    assert replaced["status"] == "match"
    assert replaced["selected_source_id"] == "two"
