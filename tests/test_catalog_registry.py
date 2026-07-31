from __future__ import annotations

from astropy.table import Table

from sdb_identity.catalog_overview import catalog_overview
from sdb_identity.catalog_registry import (
    CATALOG_PROVIDERS,
    REMOTE_CATALOG_PROVIDERS,
    SNAPSHOT_CATALOG_PROVIDERS,
    build_catalog_adapter,
)
from sdb_identity.reference_store import ReferenceStore


def test_registry_covers_remote_and_snapshot_catalogs():
    assert len(CATALOG_PROVIDERS) == 13
    assert REMOTE_CATALOG_PROVIDERS == ("2mass", "allwise", "gaia_dr3", "tycho2")
    assert set(SNAPSHOT_CATALOG_PROVIDERS) == {
        "gaspar13", "v70a", "iras_psc", "iras_fsc", "hip2", "tdsc",
        "ubvmeans", "paunzen15", "koen10",
    }
    assert CATALOG_PROVIDERS["tycho2"].science_tables == (
        "I/259/tyc2", "I/259/suppl_1",
    )
    assert "suppl_2" in CATALOG_PROVIDERS["tycho2"].caveats[0]


def test_registry_constructs_remote_adapter():
    adapter = build_catalog_adapter("gaia_dr3")
    assert adapter.name == "gaia_dr3"


class _GasparClient:
    provider = "test"

    def fetch_tables(self, catalog):
        assert catalog == "J/ApJ/768/25"
        science = Table({
            "Name": ["HD 1"], "_RA": [1.0], "_DE": [2.0],
            "F70": [10.0], "e_F70": [1.0], "r_Age": ["1"],
        })
        science.meta["name"] = "J/ApJ/768/25/table2"
        refs = Table({"Ref": [1], "BibCode": ["example"]})
        refs.meta["name"] = "J/ApJ/768/25/refs"
        return [science, refs]

    def fetch_readme(self, catalog):
        return "Gaspar test ReadMe"

    def source_url(self, catalog):
        return f"https://example.test/{catalog}"


def test_overview_combines_registry_with_reference_state(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch("gaspar13", _GasparClient())

    report = catalog_overview(store)
    assert report["provider_count"] == 13
    assert report["remote_count"] == 4
    assert report["snapshot_current_count"] == 1
    gaspar = next(row for row in report["providers"] if row["key"] == "gaspar13")
    assert gaspar["status"] == "current"
    assert gaspar["snapshot"]["row_count"] == 2
    assert gaspar["retained_tables"] == ["J/ApJ/768/25/refs"]
