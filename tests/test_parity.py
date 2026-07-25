from __future__ import annotations

import csv
from pathlib import Path

from astropy.table import Table

from sdb_identity.cli import main
from sdb_identity.parity import compare_exports


DATA = Path(__file__).parent / "data"


def _write(path, rows, *, main_id="HD 1"):
    table = Table(
        rows=rows,
        names=(
            "Band", "Phot", "Err", "Sys", "Lim", "Unit", "bibcode",
            "Note1", "Note2", "SourceID", "private", "exclude",
        ),
    )
    table.meta["keywords"] = {
        "id": {"value": "sdb-v3-test"},
        "main_id": {"value": main_id},
        "raj2000": {"value": 10.0}, "dej2000": {"value": -20.0},
    }
    table.write(path, format="ascii.ipac")


def test_supplied_legacy_exports_are_readable_and_cover_manifest():
    with (DATA / "parity_targets.csv").open(newline="") as stream:
        targets = list(csv.DictReader(stream))
    legacy_targets = [row for row in targets if row["case"] != "coordinate_only"]
    assert len(legacy_targets) == 9
    for target in legacy_targets:
        path = DATA / f"{target['legacy_sdbid']}-rawphot.txt"
        table = Table.read(path, format="ascii.ipac")
        assert len(table) > 0
        assert table.meta["keywords"]["id"]["value"] == target["legacy_sdbid"]


def test_compare_exports_reports_semantic_changes_without_judging_them(tmp_path):
    legacy = tmp_path / "legacy.txt"
    current = tmp_path / "current.txt"
    _write(legacy, [
        ("2MJ", 7.1, 0.02, 0.01, 0, "mag", "old", "", "", "A", 0, 0),
        ("OLD", 1.0, 0.1, 0.0, 1, "Jy", "old", "", "", "B", 0, 1),
    ])
    _write(current, [
        ("2MJ", 7.2, 0.02, 0.01, 0, "mag", "new", "", "", "A", 0, 1),
        ("NEW", 2.0, 0.2, 0.0, 0, "Jy", "new", "", "", "C", 0, 0),
    ], main_id="HD 1 updated")

    result = compare_exports(legacy, current)
    assert result["review_required"] is True
    assert result["metadata_changes"] == [{
        "field": "main_id", "legacy": "HD 1", "current": "HD 1 updated",
    }]
    assert {item["kind"] for item in result["observations"]} == {
        "changed_row", "legacy_only_row", "current_only_row",
    }
    changed = next(item for item in result["observations"] if item["kind"] == "changed_row")
    assert changed["review"] == "unreviewed"
    assert set(changed["changed_fields"]) == {"Phot", "bibcode", "exclude"}


def test_compare_export_cli(tmp_path, capsys):
    legacy = tmp_path / "legacy.txt"
    current = tmp_path / "current.txt"
    row = ("2MJ", 7.1, 0.02, 0.01, 0, "mag", "ref", "", "", "A", 0, 0)
    _write(legacy, [row])
    _write(current, [row])
    assert main(["maintenance", "compare-export", str(legacy), str(current)]) == 0
    assert '"review_required": false' in capsys.readouterr().out
