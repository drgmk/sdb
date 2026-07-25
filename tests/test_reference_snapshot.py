from __future__ import annotations

import json

import astropy.units as u
from astropy.table import Column, Table
from sqlalchemy import select

from sdb_identity.catalogs import CatalogCandidate, CatalogQueryContext, CatalogService
from sdb_identity.cli import main
from sdb_identity.database import init_database, make_session_factory
from sdb_identity.export import export_ipac
from sdb_identity.models import CatalogAttribute, CatalogRun, ExternalIdentifier, RawCatalogRow
from sdb_identity.providers import Astrometry
from sdb_identity.reference import (
    GASPAR_CATALOG,
    GASPAR_MAIN_TABLE,
    GASPAR_REFS_TABLE,
    GasparSnapshotAdapter,
    ReferenceApplicationService,
    ReferenceStore,
    V70A_CATALOG,
    V70A_MAIN_TABLE,
    V70ASnapshotAdapter,
    IRAS_PSC_CATALOG,
    IRAS_PSC_MAIN_TABLE,
    IrasPscSnapshotAdapter,
    KOEN10_CATALOG,
    KOEN10_MAIN_TABLE,
    Koen10SnapshotAdapter,
    PAUNZEN15_CATALOG,
    PAUNZEN15_MAIN_TABLE,
    Paunzen15SnapshotAdapter,
    UBVMEANS_CATALOG,
    UBVMEANS_MAIN_TABLE,
    UbvMeansSnapshotAdapter,
)
from sdb_identity.adapters.reference import TdscSnapshotAdapter
from sdb_identity.reference_definitions import V70A_DEFINITION
from sdb_identity.service import AddRequest, IdentityService, normalize_identifier


class FakeSnapshotClient:
    def __init__(self, *, flux=12.5, include_row=True):
        self.flux = flux
        self.include_row = include_row
        self.fetch_count = 0

    def fetch_tables(self, catalog):
        assert catalog == GASPAR_CATALOG
        self.fetch_count += 1
        main = Table()
        values = (lambda value: [value] if self.include_row else [])
        main.add_column(Column(values(1), name="recno", description="Record number", meta={"ucd": "RECORD"}))
        main.add_column(Column(values("HD 000038"), name="Name", description="Star name", meta={"ucd": "ID_MAIN"}))
        main.add_column(Column(values("1, 2"), name="r_Age", description="Reference (see refs.dat file)", meta={"ucd": "REFER_CODE"}))
        main.add_column(Column(values(10.0), name="_RA", unit=u.deg, description="Right ascension", meta={"ucd": "POS_EQ_RA_MAIN"}))
        main.add_column(Column(values(-20.0), name="_DE", unit=u.deg, description="Declination", meta={"ucd": "POS_EQ_DEC_MAIN"}))
        main.add_column(Column(values(self.flux), name="F70", unit=u.mJy, description="70um measured flux", meta={"ucd": "PHOT_FLUX_IR_60"}))
        main.add_column(Column(values(1.2), name="e_F70", unit=u.mJy, description="70um error", meta={"ucd": "ERROR"}))
        main.add_column(Column(values(3.4), name="chi70", description="70um excess chi"))
        main.add_column(Column(values(2), name="q_Age", description="Age reliability flag"))
        main.meta = {"name": GASPAR_MAIN_TABLE, "description": "Photometry"}

        refs = Table(rows=[
            (1, "1991ApJS...76..383D", "Duncan et al."),
            (2, "2000A&A...1A", "Example et al."),
        ], names=("Ref", "BibCode", "Aut"))
        refs["Ref"].description = "Reference code"
        refs["Ref"].meta["ucd"] = "REFER_CODE"
        refs["BibCode"].description = "Bibcode"
        refs["BibCode"].meta["ucd"] = "REFER_BIBCODE"
        refs.meta = {"name": GASPAR_REFS_TABLE, "description": "References"}
        return [main, refs]

    def fetch_readme(self, catalog):
        return "Byte-by-byte Description of file: table2.dat\nNote (6): age reliability"


class FakeV70AClient:
    def fetch_tables(self, catalog):
        assert catalog == V70A_CATALOG
        table = Table()
        table["Name"] = ["Gl 1"]
        table["HD"] = [225213]
        table["DM"] = ["BD-37 15492"]
        table["Giclas"] = [""]
        table["LHS"] = [1]
        table["OtherName"] = ["HIP 439"]
        table["_RA.icrs"] = ["00 05 24.4"]
        table["_DE.icrs"] = ["-37 21 27"]
        table["Vmag"] = [8.56]
        table["B-V"] = [1.46]
        table["U-B"] = [1.1]
        table["R-I"] = [0.8]
        table["Sp"] = ["M2V"]
        table.meta = {"name": V70A_MAIN_TABLE, "description": "The Catalogue"}
        for column in table.itercols():
            column.description = f"Description of {column.name}"
        return [table]

    def fetch_readme(self, catalog):
        return "Gliese Catalogue of Nearby Stars"


class FakeIrasPscClient:
    def fetch_tables(self, catalog):
        assert catalog == IRAS_PSC_CATALOG
        table = Table()
        table["IRAS"] = ["00001+0001"]
        table["RAh"] = [0]
        table["RAm"] = [0]
        table["RAds"] = [100]
        table["DE-"] = ["+"]
        table["DEd"] = [0]
        table["DEm"] = [1]
        table["DEs"] = [0]
        table["Major"] = [16]
        table["Minor"] = [4]
        table["PosAng"] = [0]
        for wavelength, flux, quality, error in (
            (12, 1.0, 3, 10), (25, 2.0, 2, 20),
            (60, 3.0, 1, 30), (100, 4.0, 3, 40),
        ):
            table[f"Fnu_{wavelength}"] = [flux]
            table[f"q_Fnu_{wavelength}"] = [quality]
            table[f"e_Fnu_{wavelength}"] = [error]
        table["Confuse"] = ["2"]
        table.meta = {"name": IRAS_PSC_MAIN_TABLE, "description": "IRAS PSC"}
        return [table]

    def fetch_readme(self, catalog):
        return "IRAS Point Source Catalog"


class FakeNewOpticalClient:
    def __init__(self, catalog):
        self.catalog = catalog

    def fetch_tables(self, catalog):
        assert catalog == self.catalog
        if catalog == UBVMEANS_CATALOG:
            table = Table({
                "LID": ["0100000001"], "m_LID": ["D"],
                "SimbadName": ["HD 123"], "_RA": [10.0], "_DE": [-20.0],
                "Vmag": [6.1], "e_Vmag": [0.01], "n_Vmag": [">"], "o_Vmag": [4],
                "B-V": [0.5], "e_B-V": [0.02], "n_B-V": [""], "o_B-V": [3],
                "U-B": [-0.1], "e_U-B": [0.03], "n_U-B": ["S"], "o_U-B": [2],
            })
            name = UBVMEANS_MAIN_TABLE
        elif catalog == PAUNZEN15_CATALOG:
            table = Table({
                "TYC1": [123], "TYC2": [456], "TYC3": [1],
                "TYC": ["TYC 123-456-1"], "RAICRS": [10.0], "DEICRS": [-20.0],
                "Vmag": [-9.999], "b-y": [0.21], "e_b-y": [0.01], "o_b-y": [5],
                "m1": [0.12], "e_m1": [0.02], "o_m1": [4],
                "c1": [0.34], "e_c1": [0.03], "o_c1": [3],
                "beta": [2.66], "e_beta": [0.01], "o_beta": [2],
            })
            name = PAUNZEN15_MAIN_TABLE
        else:
            table = Table({
                "HIP": [123], "_RA": [10.0], "_DE": [-20.0],
                "Vmag": [7.1], "B-V": [0.6], "U-B": [0.1],
                "V-Rc": [0.3], "V-Ic": [0.7], "n": [7],
                "Var": ["C"], "Mlt": ["G"], "SpType": ["G2V"],
            })
            name = KOEN10_MAIN_TABLE
        table.meta = {"name": name, "description": "test catalogue"}
        return [table]

    def fetch_readme(self, catalog):
        return f"ReadMe for {catalog}"


def test_snapshot_preserves_tables_schema_readme_and_relationships(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    fetched = store.fetch_gaspar(FakeSnapshotClient())
    assert (fetched.table_count, fetched.row_count, fetched.unchanged) == (2, 3, False)
    assert store.fetch_gaspar(FakeSnapshotClient()).unchanged is True

    described = store.describe()
    assert [item["name"] for item in described] == [GASPAR_MAIN_TABLE, GASPAR_REFS_TABLE]
    f70 = next(column for column in described[0]["columns"] if column["name"] == "F70")
    assert (f70["unit"], f70["ucd"], f70["description"]) == (
        "mJy", "PHOT_FLUX_IR_60", "70um measured flux",
    )
    snapshot = store.current_snapshot("gaspar13")
    assert "Note (6)" in snapshot.readme
    relationship = store.relationships()[0]
    assert (relationship.from_column, relationship.to_column, relationship.parser) == (
        "r_Age", "Ref", "comma_separated_ints",
    )
    assert store.rows("refs")[0]["BibCode"] == "1991ApJS...76..383D"


def test_reference_fetch_can_reuse_generic_snapshot_cache(tmp_path):
    reference = tmp_path / "reference.sqlite"
    cache = tmp_path / "cache.sqlite"
    client = FakeSnapshotClient()
    store = ReferenceStore(reference)

    first = store.fetch("gaspar13", client, cache_path=cache)
    assert first.unchanged is False
    assert client.fetch_count == 1

    second = ReferenceStore(reference).fetch(
        "gaspar13", client, cache_path=cache
    )
    assert second.unchanged is True
    assert client.fetch_count == 1
    f70 = next(
        column for column in store.describe()[0]["columns"]
        if column["name"] == "F70"
    )
    assert (f70["unit"], f70["ucd"], f70["description"]) == (
        "mJy", "PHOT_FLUX_IR_60", "70um measured flux",
    )


def test_gaspar_adapter_matches_locally_and_resolves_references(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch_gaspar(FakeSnapshotClient())
    adapter = GasparSnapshotAdapter(store)
    candidates = adapter.query(CatalogQueryContext(
        1, "sdbid", Astrometry(10.0, -20.0, 2000.0), ("HD 38",),
    ))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_id == "HD 000038"
    assert [item["Ref"] for item in candidate.payload["_resolved_age_references"]] == [1, 2]
    measurement = candidate.measurements[0]
    assert (measurement.band, measurement.value, measurement.error) == ("MIPS70", 12.5, 1.2)
    assert measurement.systematic_error == 0.625


def test_v70a_uses_generic_snapshot_store_and_alias_matching(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    fetched = store.fetch("v70a", FakeV70AClient())
    assert (fetched.catalog, fetched.table_count, fetched.row_count) == (
        V70A_CATALOG, 1, 1,
    )
    described = store.describe(adapter="v70a")
    assert described[0]["name"] == V70A_MAIN_TABLE
    adapter = V70ASnapshotAdapter(store)
    candidates = adapter.query(CatalogQueryContext(
        1,
        "sdbid",
        Astrometry(1.3516667, -37.3575, 2000.0),
        ("HD 225213",),
    ))
    assert len(candidates) == 1
    assert candidates[0].source_id == "Gl 1"
    assert candidates[0].measurements == ()
    assert candidates[0].payload["_legacy_disabled_bands"]["VJ"] == "Vmag"
    attributes = {value.key: value for value in candidates[0].attributes}
    assert attributes["spectral_type"].value_text == "M2V"
    assert attributes["v_magnitude"].value_float == 8.56
    assert attributes["v_magnitude"].unit == "mag"
    assert candidates[0].payload["_sdb_association"] == {
        "method": "position+identifier",
        "identifier_agreement": True,
        "matched_identifiers": ["HD 225213"],
        "catalog_identifiers": ["BD-37 15492", "GL 1", "HD 225213", "HIP 439", "LHS 1"],
    }


def test_v70a_normalizes_wo_and_nn_name_prefixes_to_gj():
    assert "GJ 9006" in V70A_DEFINITION.identifiers({"Name": "Wo 9006"})
    assert "GJ 12 A" in V70A_DEFINITION.identifiers({"Name": "NN 0012A"})


def test_v70a_identifier_evidence_can_resolve_stale_coordinates(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch("v70a", FakeV70AClient())
    adapter = V70ASnapshotAdapter(store)
    candidate = adapter.query(CatalogQueryContext(
        1,
        "sdbid",
        Astrometry(1.3516667, -37.3475, 2000.0),
        ("HD 225213",),
    ))[0]
    assert adapter.score_candidate(None, candidate, 36.0) == 1.0
    assert adapter.score_candidate(None, candidate, 121.0) == 0.0


def test_tdsc_component_specific_ids_outweigh_shared_hip():
    primary = CatalogCandidate(
        "primary", 10, -20, 2000,
        {"_sdb_association": {
            "matched_identifiers": ["HD 224953", "HIP 169"],
        }},
    )
    companion = CatalogCandidate(
        "companion", 10, -20, 2000,
        {"_sdb_association": {"matched_identifiers": ["HIP 169"]}},
    )
    assert TdscSnapshotAdapter.score_candidate(None, primary, 0.2) == 1.0
    assert TdscSnapshotAdapter.score_candidate(None, companion, 4.2) < 0.5


def test_iras_psc_full_snapshot_normalizes_resolution_quality_and_ellipse(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    fetched = store.fetch("iras_psc", FakeIrasPscClient())
    assert (fetched.row_count, fetched.table_count) == (1, 1)
    adapter = IrasPscSnapshotAdapter(store)
    ra, dec = adapter.definition.position(store.rows("main", adapter="iras_psc")[0])
    candidates = adapter.query(CatalogQueryContext(
        1, "sdbid", Astrometry(ra, dec, 1983.5), (),
    ))
    assert len(candidates) == 1
    assert candidates[0].payload["Major"] == 16
    values = {value.band: value for value in candidates[0].measurements}
    assert values["IRAS12"].resolution_major_arcsec == 30.0
    assert values["IRAS100"].resolution_major_arcsec == 120.0
    assert values["IRAS25"].error == 0.4
    assert values["IRAS60"].upper_limit is True
    assert values["IRAS12"].blend_state == "blended"
    assert values["IRAS12"].blend_reason == "provider_flagged"


def test_ubvmeans_marks_d_as_an_unresolved_multiple_in_the_aperture(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch("ubvmeans", FakeNewOpticalClient(UBVMEANS_CATALOG))
    candidate = UbvMeansSnapshotAdapter(store).query(CatalogQueryContext(
        1, "sdbid", Astrometry(10.0, -20.0, 2000.0), ("HD 123",),
    ))[0]
    values = {value.band: value for value in candidate.measurements}
    assert set(values) == {"VJ", "BJ_VJ", "UJ_BJ"}
    assert values["VJ"].ownership_scope == "system"
    assert values["VJ"].blend_state == "blended"
    assert values["VJ"].blend_reason == "catalog_multiple_in_aperture"
    assert values["UJ_BJ"].quality == "S"
    assert values["BJ_VJ"].resolution_major_arcsec is None


def test_paunzen_native_indices_use_spatial_limit_not_aperture(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch("paunzen15", FakeNewOpticalClient(PAUNZEN15_CATALOG))
    candidate = Paunzen15SnapshotAdapter(store).query(CatalogQueryContext(
        1, "sdbid", Astrometry(10.0, -20.0, 2000.0), ("TYC 123-456-1",),
    ))[0]
    values = {value.band: value for value in candidate.measurements}
    assert set(values) == {"BS_YS", "STROMM1", "STROMC1"}
    assert values["BS_YS"].resolution_major_arcsec == 0.8
    assert values["BS_YS"].resolution_kind == "catalog_spatial_resolution_limit"
    assert values["STROMM1"].note2 == "beta:2.66"


def test_koen_optical_photometry_records_aperture_and_flags(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch("koen10", FakeNewOpticalClient(KOEN10_CATALOG))
    candidate = Koen10SnapshotAdapter(store).query(CatalogQueryContext(
        1, "sdbid", Astrometry(10.0, -20.0, 2000.0), ("HIP 123",),
    ))[0]
    values = {value.band: value for value in candidate.measurements}
    assert set(values) == {"VJ", "BJ_VJ", "UJ_BJ", "VJ_RC", "VJ_IC"}
    assert values["VJ"].resolution_major_arcsec == 30.0
    assert values["VJ"].resolution_kind == "photometric_aperture_diameter"
    assert values["VJ"].note2 == "multiplicity:G"
    attributes = {value.key: value.value_text for value in candidate.attributes}
    assert attributes == {
        "variability_flag": "C", "multiplicity_flag": "G", "spectral_type": "G2V",
    }


def test_iras_ellipse_score_accepts_offset_along_major_axis(session_factory, tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch("iras_psc", FakeIrasPscClient())
    adapter = IrasPscSnapshotAdapter(store)
    ra, dec = adapter.definition.position(store.rows("main", adapter="iras_psc")[0])
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=ra, dec_deg=dec + 8.0 / 3600.0, epoch=1983.5)
    )
    result = CatalogService(
        session_factory, {"iras_psc": adapter}
    ).refresh(target.sdbid, "iras_psc")
    assert result.status == "match"
    with session_factory() as session:
        raw = session.scalar(select(RawCatalogRow))
        assert raw.separation_arcsec > 7.9
        assert raw.score > 0.8


def test_iras_ellipse_score_uses_orientation_and_handles_ra_wrap():
    candidate = CatalogCandidate(
        "IRAS test", 359.999, 0.0, 1983.5,
        {"Major": 20, "Minor": 2, "PosAng": 90},
    )
    east = CatalogQueryContext(
        1, "east", Astrometry(0.001, 0.0, 1983.5), (),
    )
    north = CatalogQueryContext(
        2, "north", Astrometry(359.999, 8.0 / 3600.0, 1983.5), (),
    )
    assert IrasPscSnapshotAdapter.score_candidate(east, candidate, 7.2) > 0.9
    assert IrasPscSnapshotAdapter.score_candidate(north, candidate, 8.0) < 0.001


def test_snapshot_alias_index_finds_alternate_identifier_without_spatial_scan(tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch("v70a", FakeV70AClient())
    adapter = V70ASnapshotAdapter(store)
    candidates = adapter.query(CatalogQueryContext(
        1, "sdbid", Astrometry(180.0, 50.0, 2000.0), ("HD 225213",),
    ))
    assert [candidate.source_id for candidate in candidates] == ["Gl 1"]


def test_v70a_attributes_are_versioned_with_selected_run(session_factory, tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch("v70a", FakeV70AClient())
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=1.3516667, dec_deg=-37.3575)
    )
    result = CatalogService(
        session_factory, {"v70a": V70ASnapshotAdapter(store)}
    ).refresh(target.sdbid, "v70a")
    assert result.status == "match"
    with session_factory() as session:
        values = list(session.scalars(
            select(CatalogAttribute)
            .join(CatalogRun, CatalogRun.id == CatalogAttribute.run_id)
            .where(CatalogRun.is_current.is_(True))
        ))
        attributes = {value.key: value for value in values}
        assert attributes["spectral_type"].value_text == "M2V"
        assert attributes["v_magnitude"].value_float == 8.56
        assert attributes["v_magnitude"].raw_row_id is not None


def test_attributes_cli_lists_current_v70a_values(tmp_path, capsys):
    database = tmp_path / "sdb.sqlite"
    reference = tmp_path / "reference.sqlite"
    init_database(database)
    store = ReferenceStore(reference)
    store.fetch("v70a", FakeV70AClient())
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(
        AddRequest(ra_deg=1.3516667, dec_deg=-37.3575)
    )
    CatalogService(sessions, {"v70a": V70ASnapshotAdapter(store)}).refresh(
        target.sdbid, "v70a"
    )
    assert main([
        "--database", str(database), "attributes", target.sdbid,
        "--key", "spectral_type",
    ]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["provider"] == "v70a"
    assert value["value_text"] == "M2V"


def test_gaspar_refresh_copies_selected_snapshot_row_into_main_sdb(session_factory, tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch_gaspar(FakeSnapshotClient())
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    with session_factory() as session, session.begin():
        session.add(ExternalIdentifier(
            target_id=target.target_id,
            value="HD 38",
            normalized_value=normalize_identifier("HD 38"),
            source="test",
        ))
    result = CatalogService(
        session_factory, {"gaspar13": GasparSnapshotAdapter(store)}
    ).refresh(target.sdbid, "gaspar13")
    assert (result.status, result.measurement_count) == ("match", 1)
    with session_factory() as session:
        raw = session.scalar(select(RawCatalogRow))
        payload = json.loads(raw.payload_json)
        assert payload["_resolved_age_references"][0]["BibCode"] == "1991ApJS...76..383D"


def test_reference_cli_describes_and_refreshes_local_snapshot(tmp_path, capsys):
    database = tmp_path / "sdb.sqlite"
    reference = tmp_path / "reference.sqlite"
    init_database(database)
    store = ReferenceStore(reference)
    store.fetch_gaspar(FakeSnapshotClient())
    sessions = make_session_factory(database)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10, dec_deg=-20))

    common = ["--database", str(database), "--reference-database", str(reference)]
    assert main([*common, "reference", "describe", "gaspar13", "table2"]) == 0
    described = json.loads(capsys.readouterr().out)
    assert described["row_count"] == 1
    assert main([*common, "reference", "relationships", "gaspar13"]) == 0
    relationship = json.loads(capsys.readouterr().out)
    assert relationship["from_column"] == "r_Age"
    assert main([*common, "reference", "references", "gaspar13"]) == 0
    reference_row = json.loads(capsys.readouterr().out.splitlines()[0])
    assert reference_row["BibCode"] == "1991ApJS...76..383D"
    assert main([*common, "reference", "readme", "gaspar13"]) == 0
    assert "Note (6)" in capsys.readouterr().out

    assert main([
        *common, "--offline", "refresh", target.sdbid, "--provider", "gaspar13",
    ]) == 0
    refreshed = json.loads(capsys.readouterr().out)
    assert refreshed["status"] == "match"

    assert main([*common, "reference", "apply", "gaspar13", "--all"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert (applied["targets"], applied["matched"]) == (1, 1)
    assert main([*common, "reference", "application-status", "gaspar13"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "completed"
    assert main([*common, "reference", "pending", "gaspar13"]) == 0
    pending = json.loads(capsys.readouterr().out)
    assert pending["sdbid"] == target.sdbid


def test_bulk_application_is_idempotent_and_refreshes_changed_rows(session_factory, tmp_path):
    store = ReferenceStore(tmp_path / "reference.sqlite")
    store.fetch_gaspar(FakeSnapshotClient(flux=12.5))
    matched = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    unmatched = IdentityService(session_factory).add(AddRequest(ra_deg=30, dec_deg=-40))
    service = ReferenceApplicationService(session_factory, store)

    first = service.apply_gaspar()
    assert (first.targets, first.refreshed, first.matched, first.no_match) == (2, 2, 1, 1)
    assert first.unmatched_rows == 0
    export_ipac(session_factory, matched.sdbid, tmp_path / "bulk.txt")
    assert all(target.id != matched.target_id for _, target, _ in service.pending())
    repeated = service.apply_gaspar()
    assert repeated.unchanged is True
    assert repeated.refreshed == 0

    new_target = IdentityService(session_factory).add(AddRequest(ra_deg=50, dec_deg=-10))
    with_new_target = service.apply_gaspar()
    assert with_new_target.refreshed == 1
    assert with_new_target.no_match == 1

    store.fetch_gaspar(FakeSnapshotClient(flux=13.5))
    changed = service.apply_gaspar()
    assert (changed.refreshed, changed.matched) == (1, 1)
    with session_factory() as session:
        from sdb_identity.models import ExportDirtyTarget, NormalizedMeasurement
        current = session.scalar(
            select(NormalizedMeasurement)
            .join(CatalogRun, CatalogRun.id == NormalizedMeasurement.run_id)
            .where(
                NormalizedMeasurement.target_id == matched.target_id,
                CatalogRun.provider == "gaspar13",
                CatalogRun.is_current.is_(True),
            )
        )
        assert current.value == 13.5
        dirty_targets = {
            value.target_id for value in session.scalars(
                select(ExportDirtyTarget).where(ExportDirtyTarget.source_type == "reference")
            )
        }
        assert matched.target_id in dirty_targets
        assert unmatched.target_id in dirty_targets
        assert new_target.target_id in dirty_targets

    store.fetch_gaspar(FakeSnapshotClient(include_row=False))
    removed = service.apply_gaspar()
    assert (removed.refreshed, removed.no_match, removed.catalog_rows) == (1, 1, 0)
    with session_factory() as session:
        current_measurements = session.scalars(
            select(NormalizedMeasurement)
            .join(CatalogRun, CatalogRun.id == NormalizedMeasurement.run_id)
            .where(
                NormalizedMeasurement.target_id == matched.target_id,
                CatalogRun.provider == "gaspar13",
                CatalogRun.is_current.is_(True),
            )
        ).all()
        assert current_measurements == []
