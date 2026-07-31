from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from sdb_identity.catalogs import CatalogService
from sdb_identity.adapters.allwise import AllWiseAdapter
from sdb_identity.cli import main
from sdb_identity.database import make_session_factory
from sdb_identity.metadata import MetadataQueryResult, MetadataService
from sdb_identity.models import NormalizedMeasurement
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.update import UpdateSummary
from tests.fakes import FakeGaia, FakeSimbad, astrometry, simbad_result
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

    assert main(["--database", str(database), "--offline", "hierarchy", "status", added["sdbid"], "--scope", "provider"]) == 0
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
        "--database", str(database), "hierarchy", "review-queue", "--view", "blend", "--all"
    ]) == 0
    table = capsys.readouterr().out

    assert "sdbid" in table
    assert "class" in table
    assert "assignment" in table
    assert added["sdbid"] in table
    assert "single_or_unknown" in table

    assert main([
        "--database", str(database), "hierarchy", "review-queue", "--view", "blend",
        "--all", "--format", "jsonl",
    ]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert rows[0]["sdbid"] == added["sdbid"]
    assert rows[0]["target_level"] == "single_or_unknown"
    assert rows[0]["measurement_count"] == 0

    assert main([
        "--database", str(database), "hierarchy", "review-queue", "--view", "blend",
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


def test_cli_add_ensure_uses_configured_providers(
    tmp_path, capsys, monkeypatch,
):
    database = tmp_path / "cli.sqlite"
    reference = tmp_path / "reference.sqlite"
    config = tmp_path / "sdb.toml"
    config.write_text(
        "[catalog]\n"
        'providers = ["gaia_dr3", "2mass"]\n'
    )
    main(["--database", str(database), "init"])
    capsys.readouterr()
    calls = []

    class FakeUpdateService:
        def update_targets(self, targets, *, providers, force):
            calls.append((tuple(targets), tuple(providers), force))
            return UpdateSummary(
                target_count=len(tuple(targets)),
                refreshed=0,
                skipped=0,
                missing=0,
                failed=0,
                items=(),
            )

    monkeypatch.setattr(
        "sdb_identity.live_providers.AstroquerySimbad",
        lambda: FakeSimbad({
            "HD 1": simbad_result(
                "HD   1",
                astrometry(10.0, -20.0, source="simbad"),
            ),
        }),
    )
    monkeypatch.setattr(
        "sdb_identity.live_providers.AstroqueryGaia",
        FakeGaia,
    )
    monkeypatch.setattr(
        "sdb_identity.cli._update_service",
        lambda *args, **kwargs: FakeUpdateService(),
    )

    assert main([
        "--config", str(config),
        "--database", str(database),
        "--reference-database", str(reference),
        "add", "HD 1", "--ensure",
    ]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["created_count"] == 1
    assert result["providers"] == ["simbad", "gaia_dr3", "2mass"]
    assert calls == [(
        (result["items"][0]["sdbid"],),
        ("simbad", "gaia_dr3", "2mass"),
        False,
    )]


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

    assert main(["--database", str(database), "runs", target.sdbid, "--provider", "2mass"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["kind"] == "catalog"
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

    assert main(["--database", str(database), "runs", target.sdbid, "--provider", "simbad"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["kind"] == "metadata"
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


def test_cli_update_routes_provider_chatter_to_stderr(
    tmp_path, capsys, monkeypatch,
):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    main([
        "--database", str(database), "--offline", "add",
        "--ra", "10", "--dec", "-20",
    ])
    target = json.loads(capsys.readouterr().out)

    class NoisyUpdateService:
        def update_target(self, *args, **kwargs):
            print("third-party provider status")
            return UpdateSummary(
                target_count=1,
                refreshed=1,
                skipped=0,
                missing=0,
                failed=0,
                items=(),
            )

    monkeypatch.setattr(
        "sdb_identity.cli._update_service",
        lambda *args, **kwargs: NoisyUpdateService(),
    )
    assert main([
        "--database", str(database), "update", target["sdbid"],
        "--providers", "2mass",
    ]) == 0
    captured = capsys.readouterr()

    assert json.loads(captured.out)["refreshed"] == 1
    assert "third-party provider status" not in captured.out
    assert "third-party provider status" in captured.err


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
    CatalogService(sessions, {
        "allwise": FakeCatalog(
            [candidate(
                "cli-wise",
                measurements=[measurement("WISE3P4", 8.1)],
            )],
            name="allwise",
            release="test",
        ),
    }).refresh(target.sdbid, "allwise")
    with sessions() as session:
        measurement_id = session.scalar(select(NormalizedMeasurement.id))
    assert main([
        "--database", str(database), "photometry", "exclude",
        str(measurement_id), "--actor", "grant",
        "--reason", "blended",
    ]) == 0
    excluded = json.loads(capsys.readouterr().out)
    assert excluded["excluded"] is True
    assert main([
        "--database", str(database), "photometry", "overrides", target.sdbid,
    ]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["reason"] == "blended"



def test_cli_photometry_review_lists_measurements(tmp_path, capsys):
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
    assert rows[0]["band"] == "WISE3P4"


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
        "--database", str(database), "photometry", "review-queue",
        "--sample", "science",
    ]) == 0
    table = capsys.readouterr().out
    assert "priority" in table
    assert target.sdbid in table

    assert main([
        "--database", str(database), "photometry", "review-queue",
        "--sample", "science", "--format", "jsonl",
    ]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["sdbid"] == target.sdbid
    assert "current_decision" not in rows[0]

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

    output_dir = tmp_path / "reviews"
    assert main([
        "--database", str(database), "photometry", "review-html",
        "--sample", "science", "--output-dir", str(output_dir),
    ]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["targets"] == 1
    assert result["queue_rows"] >= 1
    assert Path(result["index"]).exists()
    assert len(result["review_pages"]) == 1
    page = Path(result["review_pages"][0])
    assert page.exists()
    assert "Plotly.newPlot" in page.read_text()
    index = (output_dir / "index.html").read_text()
    assert target.sdbid in index

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
    assert {value["sdbid"] for value in reviewed} == {target.sdbid}
    chosen = next(value for value in reviewed if value["source_id"] == "two")
    assert main([
        "--database", str(database), "override-catalog-match",
        str(chosen["candidate_id"]), "--actor", "grant", "--reason", "image check",
    ]) == 0
    replaced = json.loads(capsys.readouterr().out)
    assert replaced["status"] == "match"
    assert replaced["selected_source_id"] == "two"


def _add_alias(sessions, sdbid, value):
    from sdb_identity.models import ExternalIdentifier, Target
    from sdb_identity.identifiers import normalize_identifier

    with sessions() as session:
        target_id = session.scalar(select(Target.id).where(Target.sdbid == sdbid))
        session.add(ExternalIdentifier(
            target_id=target_id, value=value,
            normalized_value=normalize_identifier(value), source="simbad",
        ))
        session.commit()


def _decode_json_objects(text):
    """Decode one or more concatenated (indented) JSON objects."""
    decoder = json.JSONDecoder()
    index = 0
    objects = []
    while index < len(text):
        while index < len(text) and text[index] in " \n\t\r":
            index += 1
        if index >= len(text):
            break
        obj, index = decoder.raw_decode(text, index)
        objects.append(obj)
    return objects


def test_cli_inspection_commands_resolve_by_alias(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogService(
        sessions,
        {"2mass": FakeCatalog([candidate(measurements=[measurement()])])},
    ).refresh(target.sdbid, "2mass")
    MetadataService(
        sessions,
        FakeMetadataProvider(MetadataQueryResult("match", (snapshot(),))),
    ).refresh(target.sdbid)
    _add_alias(sessions, target.sdbid, "TESTALIAS 77")

    assert main(["--database", str(database), "--offline", "status", "TESTALIAS 77"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["sdbid"] == target.sdbid

    assert main(["--database", str(database), "runs", "TESTALIAS 77", "--provider", "2mass"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["sdbid"] == target.sdbid
    assert row["provider"] == "2mass"

    assert main(["--database", str(database), "runs", "TESTALIAS 77", "--provider", "simbad"]) == 0
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["sdbid"] == target.sdbid
    assert metadata["provider"] == "simbad"

    assert main(["--database", str(database), "--offline", "status", "NOT A TARGET"]) == 1
    assert "target not found: NOT A TARGET" in capsys.readouterr().err


def test_cli_status_prints_every_target_sharing_an_alias(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    sessions = make_session_factory(database)
    first = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))
    second = IdentityService(sessions).add(AddRequest(ra_deg=10.5, dec_deg=-20))
    assert first.sdbid != second.sdbid
    _add_alias(sessions, first.sdbid, "SHARED SYSTEM")
    _add_alias(sessions, second.sdbid, "SHARED SYSTEM")

    assert main(["--database", str(database), "--offline", "status", "SHARED SYSTEM"]) == 0
    objects = _decode_json_objects(capsys.readouterr().out)
    assert sorted(obj["sdbid"] for obj in objects) == sorted([first.sdbid, second.sdbid])


def test_cli_review_matches_lists_only_ambiguous_submissions(tmp_path, capsys):
    database = tmp_path / "cli.sqlite"
    main(["--database", str(database), "init"])
    capsys.readouterr()
    sessions = make_session_factory(database)
    from sdb_identity.models import MatchCandidate, Submission

    with sessions() as session:
        ambiguous = Submission(input_name="AMBIG NAME", status="ambiguous")
        resolved = Submission(input_name="RESOLVED NAME", status="completed")
        session.add_all([ambiguous, resolved])
        session.flush()
        for source_id in ("gaia-a", "gaia-b"):
            session.add(MatchCandidate(
                submission_id=ambiguous.id, provider="gaia_dr3", source_id=source_id,
                ra_deg=10.0, dec_deg=-20.0, epoch=2000.0, separation_arcsec=1.0,
                score=0.5, score_details="{}", accepted=False,
            ))
        session.add(MatchCandidate(
            submission_id=resolved.id, provider="gaia_dr3", source_id="gaia-c",
            ra_deg=10.0, dec_deg=-20.0, epoch=2000.0, separation_arcsec=0.1,
            score=0.9, score_details="{}", accepted=True,
        ))
        session.commit()

    assert main(["--database", str(database), "review", "matches"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["submitted_name"] == "AMBIG NAME"
    assert row["reason"]
    assert {candidate["source_id"] for candidate in row["candidates"]} == {"gaia-a", "gaia-b"}
    assert all("candidate_id" in candidate for candidate in row["candidates"])
