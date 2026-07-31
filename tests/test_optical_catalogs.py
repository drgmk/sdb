from __future__ import annotations

import astropy.units as u
from astropy.table import Table

from sdb_identity.catalog_policy import catalog_source_display_name
from sdb_identity.adapters.reference import (
    Hip2SnapshotAdapter,
    Paunzen15SnapshotAdapter,
    TdscSnapshotAdapter,
)
from sdb_identity.adapters.tycho2 import Tycho2Adapter
from sdb_identity.catalogs import CatalogQueryContext
from sdb_identity.providers import Astrometry
from sdb_identity.reference import (
    HIP2_CATALOG,
    HIP2_MAIN_TABLE,
    PAUNZEN15_DEFINITION,
    TDSC_CATALOG,
    TDSC_MAIN_TABLE,
    TDSC_SUPPLEMENT_TABLE,
    TDSC_DEFINITION,
    ReferenceStore,
)


class FakeHip2Client:
    def fetch_tables(self, catalog):
        assert catalog == HIP2_CATALOG
        table = Table()
        table["HIP"] = [123]
        table["RArad"] = [0.2]
        table["DErad"] = [-0.1]
        table["Hpmag"] = [7.1234]
        table["e_Hpmag"] = [0.002]
        table["sHp"] = [0.01]
        table["Sn"] = [5]
        table["VA"] = [1]
        table["B-V"] = [0.6]
        table["V-I"] = [0.7]
        table.meta = {"name": HIP2_MAIN_TABLE, "description": "Hipparcos-2"}
        return [table]

    def fetch_readme(self, catalog):
        return "Hipparcos, the New Reduction"


class FakeTdscClient:
    @staticmethod
    def _table(name, tdsc, component, ra, flag):
        table = Table()
        table["TDSC"] = [tdsc]
        table["m_TDSC"] = [component]
        table["RAJ2000"] = [ra]
        table["DEJ2000"] = [-20.0]
        table["EpRA"] = [1991.2]
        table["EpDE"] = [1991.4]
        table["BTmag"] = [8.2]
        table["e_BTmag"] = [0.02]
        table["VTmag"] = [7.8]
        table["e_VTmag"] = [0.03]
        table["magflg"] = [flag]
        table["TYC1"] = [1]
        table["TYC2"] = [2]
        table["TYC3"] = [1 if component == "A" else 2]
        table["HIP"] = [123]
        table["HD"] = [456]
        table["WDS"] = ["00400-2000"]
        table.meta = {"name": name, "description": name}
        return table

    def fetch_tables(self, catalog):
        assert catalog == TDSC_CATALOG
        main = self._table(TDSC_MAIN_TABLE, 10, "A", 10.0, "")
        supplement = self._table(TDSC_SUPPLEMENT_TABLE, 10, "B", 10.01, "H")
        notes = Table({"TDSC": [10], "Text": ["example"]})
        notes.meta = {"name": f"{TDSC_CATALOG}/notes", "description": "Notes"}
        return [main, supplement, notes]

    def fetch_readme(self, catalog):
        return "Tycho Double Star Catalogue"


def test_hip2_snapshot_exports_only_native_hp(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    result = store.fetch("hip2", FakeHip2Client())
    assert result.row_count == 1
    adapter = Hip2SnapshotAdapter(store)
    coordinate = Astrometry((0.2 * u.rad).to_value(u.deg), (-0.1 * u.rad).to_value(u.deg), 1991.25)
    candidates = adapter.query(CatalogQueryContext(1, "target", coordinate, ("HIP 123",)))
    assert len(candidates) == 1
    assert candidates[0].source_id == "123"
    assert [(value.band, value.value) for value in candidates[0].measurements] == [
        ("HP", 7.1234),
    ]


def test_tdsc_snapshot_matches_main_and_supplement_tables(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    result = store.fetch("tdsc", FakeTdscClient())
    assert result.row_count == 3
    adapter = TdscSnapshotAdapter(store)
    main = adapter.query(CatalogQueryContext(
        1, "main", Astrometry(10.0, -20.0, 1991.25), ("TYC 1-2-1",),
    ))
    supplement = adapter.query(CatalogQueryContext(
        2, "supplement", Astrometry(10.01, -20.0, 1991.25), ("TYC 1-2-2",),
    ))
    assert [candidate.source_id for candidate in main] == ["10|m_TDSC=A"]
    assert main[0].payload["_sdb_photometry_scope"] == {
        "native_code": "A",
        "kind": "named_component",
        "component_label": "A",
    }
    assert catalog_source_display_name(
        "tdsc", main[0].source_id, main[0].payload,
    ) == "HD 456A"
    assert [value.band for value in main[0].measurements] == ["BT", "VT"]
    assert [candidate.source_id for candidate in supplement] == ["10|m_TDSC=B"]
    assert [value.band for value in supplement[0].measurements] == ["BT"]
    assert supplement[0].epoch == 2000.0
    assert main[0].provenance[0].table_id == TDSC_MAIN_TABLE
    assert supplement[0].provenance[0].table_id == TDSC_SUPPLEMENT_TABLE
    assert main[0].provenance[0].identifier_column == "TDSC"
    assert main[0].provenance[0].identifier_value == "10"
    assert main[0].provenance[0].access_url.endswith(
        "I%2F276%2Fcatalog&TDSC===10"
    )


def test_tdsc_component_designation_controls_identity_but_not_lookup():
    payload = {
        "TDSC": 88,
        "m_TDSC": "A",
        "HIP": 169,
        "HD": 224953,
        "WDS": "00021-6817",
        "TYC1": 9134,
        "TYC2": 1714,
        "TYC3": 1,
    }
    assert TDSC_DEFINITION.identifiers(payload) == (
        "HD 224953A",
        "TYC 9134-1714-1",
        "WDS J00021-6817A",
        "HIP 169",
    )
    assert "HD 224953" in TDSC_DEFINITION.lookup_identifiers(payload)


def test_paunzen_literal_tyc_label_is_not_used_as_a_row_locator():
    adapter = object.__new__(Paunzen15SnapshotAdapter)
    adapter.definition = PAUNZEN15_DEFINITION

    assert adapter._provenance_identifier({
        "TYC": "TYC",
        "TYC1": 9134,
        "TYC2": 1714,
        "TYC3": 1,
    }) == (None, None)


def test_tdsc_bulk_coordinates_are_propagated_per_axis_to_j2000():
    ra, dec = TDSC_DEFINITION.position({
        "RAdeg": 10.0,
        "DEdeg": 20.0,
        "EpRA": 1990.0,
        "EpDE": 1992.0,
        "pmRA": 1000.0,
        "pmDE": -500.0,
    })
    assert ra > 10.0
    assert dec < 20.0


def test_tycho2_normalizes_native_bands_and_marks_photocentres():
    candidate = Tycho2Adapter.parse_row({
        "TYC1": 1,
        "TYC2": 13,
        "TYC3": 1,
        "RAmdeg": 10.0,
        "DEmdeg": -20.0,
        "BTmag": 8.2,
        "e_BTmag": 0.02,
        "VTmag": 7.9,
        "e_VTmag": 0.03,
        "pflag": "P",
        "posflg": "P",
        "prox": 5,
    })
    assert candidate.source_id == "TYC 1-13-1"
    assert [value.band for value in candidate.measurements] == ["BT", "VT"]
    assert all(value.ownership_scope == "system" for value in candidate.measurements)
    assert all(value.blend_state == "blended" for value in candidate.measurements)
    assert all(value.blend_reason == "provider_flagged" for value in candidate.measurements)
    proximity = candidate.payload["_sdb_review"]["fields"][0]
    assert proximity == {
        "key": "nearest_source_arcsec",
        "label": "nearest catalog source",
        "value": 0.5,
        "unit": "arcsec",
        "source_column": "prox",
    }


def test_tycho2_does_not_mislabel_supplement_hp_as_vt():
    candidate = Tycho2Adapter.parse_row({
        "TYC1": 3105,
        "TYC2": 2070,
        "TYC3": 1,
        "RA(ICRS)": 279.2347,
        "DE(ICRS)": 38.7837,
        "BTmag": None,
        "VTmag": 0.087,
        "e_VTmag": 0.003,
        "mflag": "H",
        "flag": "H",
    }, table_name="I/259/suppl_1")
    assert candidate.source_id == "TYC 3105-2070-1"
    assert candidate.measurements == ()
    assert candidate.payload["_table"] == "I/259/suppl_1"
    assert candidate.provenance[0].table_id == "I/259/suppl_1"
    reconstructed = Tycho2Adapter.parse_row(candidate.payload)
    assert reconstructed.ra_deg == candidate.ra_deg
    assert reconstructed.dec_deg == candidate.dec_deg


def test_tycho2_uses_simbad_component_identifier_as_explicit_evidence():
    adapter = Tycho2Adapter()
    context = CatalogQueryContext(
        1, "target", Astrometry(10, -20), ("TYC 858-1221-1",),
    )
    component_one = adapter.parse_row({
        "TYC1": 858, "TYC2": 1221, "TYC3": 1,
        "RAmdeg": 10.0, "DEmdeg": -20.0,
    })
    component_two = adapter.parse_row({
        "TYC1": 858, "TYC2": 1221, "TYC3": 2,
        "RAmdeg": 10.0, "DEmdeg": -20.0,
    })

    assert adapter.expected_source_ids(context.identifiers) == {"858-1221-1"}
    assert adapter.score_candidate(context, component_one, 0.15) == 1.0
    assert adapter.score_candidate(context, component_two, 0.15) < 0.25


def test_tycho2_never_queries_unreliable_second_supplement():
    class RecordingClient:
        TIMEOUT = None

        def __init__(self):
            self.catalogs = []

        def query_region(self, coordinate, *, radius, catalog):
            self.catalogs.append(catalog)
            return []

    adapter = Tycho2Adapter()
    client = RecordingClient()
    adapter.create_client = lambda: client
    result = adapter.query(CatalogQueryContext(
        1, "target", Astrometry(10, -20), (),
    ))
    assert result == []
    assert client.catalogs == ["I/259/tyc2", "I/259/suppl_1"]
    assert "I/259/suppl_2" not in {
        table for table, _epoch in adapter.science_tables
    }
