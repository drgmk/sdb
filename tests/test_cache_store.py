from __future__ import annotations

import json

from astropy.table import Table

from sdb_identity.cache_store import SnapshotCache
from sdb_identity.cli import main


def test_snapshot_cache_stores_and_reuses_current_snapshot(tmp_path):
    path = tmp_path / "sdb-cache.sqlite"
    table = Table(
        rows=[("00057+4549", "AB", 1.425, 45.8167)],
        names=("WDS", "Comp", "RAJ2000", "DEJ2000"),
    )
    table.meta["name"] = "B/wds/wds"
    table.meta["description"] = "Washington Double Star test rows"

    cache = SnapshotCache(path)
    first = cache.store_snapshot(
        provider="vizier",
        catalog_id="B/wds",
        release="wds:test",
        source_url="https://example.invalid/B/wds",
        readme="ReadMe v1",
        tables=[table],
    )
    second = cache.store_snapshot(
        provider="vizier",
        catalog_id="B/wds",
        release="wds:test",
        source_url="https://example.invalid/B/wds",
        readme="ReadMe v1",
        tables=[table],
    )
    current = cache.current_snapshot("vizier", "B/wds")

    assert first.unchanged is False
    assert second.unchanged is True
    assert second.source_id == first.source_id
    assert current is not None
    assert current.source_id == first.source_id
    assert current.tables[0].name == "B/wds/wds"
    assert current.tables[0].rows[0]["WDS"] == "00057+4549"
    assert current.tables[0].metadata["columns"][0]["name"] == "WDS"
    documentation = path.parent / f"{path.name}.catalogs" / "vizier" / "B_wds"
    assert (documentation / "ReadMe").read_text() == "ReadMe v1"
    manifest = json.loads((documentation / "manifest.json").read_text())
    assert manifest["tables"] == [{
        "name": "B/wds/wds",
        "row_count": 1,
    }]


def test_snapshot_cache_summaries_and_cli_inspection(tmp_path, capsys):
    path = tmp_path / "sdb-cache.sqlite"
    table = Table(
        rows=[("Gl 1", 1.0, -2.0)],
        names=("Name", "RAJ2000", "DEJ2000"),
    )
    table["Name"].description = "Identifier"
    table.meta["name"] = "V/70A/catalog"
    table.meta["description"] = "Nearby stars"

    cache = SnapshotCache(path)
    cache.store_snapshot(
        provider="vizier",
        catalog_id="V/70A",
        release="V/70A",
        source_url="https://example.invalid/V/70A",
        readme="Gliese ReadMe",
        tables=[table],
    )

    summaries = cache.summaries()
    assert len(summaries) == 1
    assert summaries[0].catalog_id == "V/70A"
    assert summaries[0].table_count == 1
    assert summaries[0].row_count == 1

    assert main(["--cache-database", str(path), "cache", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["catalog_id"] == "V/70A"
    assert status["row_count"] == 1

    assert main([
        "--cache-database", str(path), "cache", "tables", "V/70A",
    ]) == 0
    tables = json.loads(capsys.readouterr().out)
    assert tables["table"] == "V/70A/catalog"
    assert tables["columns"][0]["description"] == "Identifier"

    assert main([
        "--cache-database", str(path), "cache", "readme", "V/70A",
    ]) == 0
    assert capsys.readouterr().out == "Gliese ReadMe\n"

    validation = cache.validate("V/70A")
    assert validation.ok is True
    assert validation.errors == ()
    assert validation.row_count == 1

    assert main([
        "--cache-database", str(path), "cache", "validate", "V/70A",
    ]) == 0
    validate_output = capsys.readouterr().out
    assert '"ok": true' in validate_output
    assert '"row_count": 1' in validate_output


def test_snapshot_cache_restores_missing_companion_readme(tmp_path):
    path = tmp_path / "sdb-cache.sqlite"
    table = Table(rows=[("x",)], names=("Name",))
    table.meta["name"] = "I/1/main"
    table.meta["description"] = "test"
    SnapshotCache(path).store_snapshot(
        provider="vizier",
        catalog_id="I/1",
        release="I/1",
        source_url="https://example.invalid/I/1",
        readme="restorable ReadMe",
        tables=[table],
    )
    documentation = (
        path.parent / f"{path.name}.catalogs" / "vizier" / "I_1"
    )
    (documentation / "ReadMe").unlink()

    SnapshotCache(path)

    assert (documentation / "ReadMe").read_text() == "restorable ReadMe"


def test_snapshot_cache_validate_reports_invalid_snapshot(tmp_path, capsys):
    path = tmp_path / "sdb-cache.sqlite"
    table = Table(names=("Name",))
    table.meta["name"] = "empty/table"

    cache = SnapshotCache(path)
    cache.store_snapshot(
        provider="vizier",
        catalog_id="empty",
        release="empty",
        source_url="",
        readme="",
        tables=[table],
    )

    validation = cache.validate("empty")
    assert validation.ok is False
    assert "missing source URL" in validation.errors
    assert "missing ReadMe" in validation.errors
    assert "empty/table: no rows" in validation.errors

    assert main([
        "--cache-database", str(path), "cache", "validate", "empty",
    ]) == 1
    validate_output = capsys.readouterr().out
    assert '"ok": false' in validate_output
    assert "empty/table: no rows" in validate_output
