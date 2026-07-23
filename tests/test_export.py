from __future__ import annotations

from astropy.table import Table

from sdb_identity.catalogs import CatalogCandidate, CatalogService, MeasurementValue
from sdb_identity.export import export_ipac
from sdb_identity.metadata import MetadataQueryResult, MetadataService
from sdb_identity.photometry import set_photometry_association_decision
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement
from tests.test_metadata import FakeMetadataProvider, snapshot


def test_export_is_sdf_compatible(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    adapter = FakeCatalog([candidate(measurements=[
        measurement("2MJ", 7.1, 0.02),
        measurement("2MH", 6.9, 0.03, excluded=True),
        measurement("2MKS", 6.8, 0.04),
    ])])
    CatalogService(session_factory, {"2mass": adapter}).refresh(target.sdbid, "2mass")
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (snapshot(),))),
    ).refresh(target.sdbid)
    output = export_ipac(session_factory, target.sdbid, tmp_path / "target-rawphot.txt")

    table = Table.read(output, format="ascii.ipac")
    assert table.colnames == [
        "Band", "Phot", "Err", "Sys", "Lim", "Unit", "bibcode",
        "Note1", "Note2", "SourceID", "private", "exclude",
    ]
    assert table.meta["keywords"]["id"]["value"] == target.sdbid
    assert table.meta["keywords"]["main_id"]["value"] == "HD 1"
    assert table.meta["keywords"]["sp_type"]["value"] == "F5V"
    assert table.meta["keywords"]["plx_value"]["value"] == 12.3
    assert table.meta["keywords"]["otype"]["value"] == "Star"

    from sdf.photometry import Photometry

    photometry = Photometry.read_sdb_file(output)
    assert list(photometry.filters) == ["2MJ", "2MH", "2MKS"]
    assert list(photometry.ignore) == [False, True, False]


def test_export_uses_only_current_successful_run(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    service = CatalogService(session_factory, {"2mass": FakeCatalog([candidate(measurements=[measurement(value=7.1)])])})
    service.refresh(target.sdbid, "2mass")
    service.adapters["2mass"] = FakeCatalog([candidate(measurements=[measurement(value=7.2)])])
    service.refresh(target.sdbid, "2mass")
    output = export_ipac(session_factory, target.sdbid, tmp_path / "target-rawphot.txt")
    table = Table.read(output, format="ascii.ipac")
    assert list(table["Phot"]) == [7.2]



def test_export_excludes_measurement_rejected_by_association_decision(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogService(session_factory, {
        "allwise": FakeCatalog([
            candidate("wise-a", measurements=[measurement("WISE3P4", 8.1)])
        ], name="allwise", release="fake-allwise"),
    }).refresh(target.sdbid, "allwise")
    set_photometry_association_decision(
        session_factory,
        target.sdbid,
        provider="allwise",
        source_id="wise-a",
        band="WISE3P4",
        scope="reject",
        actor="grant",
        reason="belongs to nearby component",
    )

    output = export_ipac(session_factory, target.sdbid, tmp_path / "reject.txt")
    table = Table.read(output, format="ascii.ipac")

    assert list(table["exclude"]) == [1]
    assert "Association reject:belongs to nearby component" in table["Note2"][0]


def test_export_ignores_non_reject_association_decision_for_now(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogService(session_factory, {
        "allwise": FakeCatalog([
            candidate("wise-a", measurements=[measurement("WISE3P4", 8.1)])
        ], name="allwise", release="fake-allwise"),
    }).refresh(target.sdbid, "allwise")
    set_photometry_association_decision(
        session_factory,
        target.sdbid,
        provider="allwise",
        source_id="wise-a",
        band="WISE3P4",
        scope="blended",
        actor="grant",
        reason="not export-active yet",
    )

    output = export_ipac(session_factory, target.sdbid, tmp_path / "blended.txt")
    table = Table.read(output, format="ascii.ipac")

    assert list(table["exclude"]) == [0]
    assert "Association reject" not in table["Note2"][0]


def test_export_uses_latest_association_decision(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogService(session_factory, {
        "allwise": FakeCatalog([
            candidate("wise-a", measurements=[measurement("WISE3P4", 8.1)])
        ], name="allwise", release="fake-allwise"),
    }).refresh(target.sdbid, "allwise")
    set_photometry_association_decision(
        session_factory, target.sdbid, provider="allwise", source_id="wise-a",
        band="WISE3P4", scope="reject", actor="grant", reason="initial reject",
    )
    set_photometry_association_decision(
        session_factory, target.sdbid, provider="allwise", source_id="wise-a",
        band="WISE3P4", scope="component", actor="grant", reason="later accepted",
    )

    output = export_ipac(session_factory, target.sdbid, tmp_path / "latest.txt")
    table = Table.read(output, format="ascii.ipac")

    assert list(table["exclude"]) == [0]
    assert "initial reject" not in table["Note2"][0]

def test_shared_catalog_source_is_excluded_from_component_exports(session_factory, tmp_path):
    first = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    second = IdentityService(session_factory).add(AddRequest(ra_deg=10.00055, dec_deg=-20))
    adapter = FakeCatalog([candidate(measurements=[measurement("IRAS12", 1.2, 0.1)])])
    service = CatalogService(session_factory, {"iras_psc": adapter})
    service.refresh(first.sdbid, "iras_psc")
    service.refresh(second.sdbid, "iras_psc")

    output = export_ipac(session_factory, first.sdbid, tmp_path / "shared.txt")
    table = Table.read(output, format="ascii.ipac")
    assert list(table["exclude"]) == [1]
    assert "shared catalog source" in table["Note2"][0]


def test_tdsc_component_photometry_takes_precedence_over_tycho2(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    tycho = FakeCatalog(
        [CatalogCandidate(
            "TYC 1-2-1", 10, -20, 2000, {},
            (
                MeasurementValue("BT", 8.3, 0.03, unit="mag"),
                MeasurementValue("VT", 8.0, 0.03, unit="mag"),
            ),
        )],
        name="tycho2",
        release="I/259",
        query_epoch=2000,
    )
    tdsc = FakeCatalog(
        [CatalogCandidate(
            "10|m_TDSC=A", 10, -20, 1991.25, {},
            (MeasurementValue("BT", 8.2, 0.02, unit="mag"),),
        )],
        name="tdsc",
        release="I/276/catalog",
        query_epoch=1991.25,
    )
    CatalogService(session_factory, {"tycho2": tycho}).refresh(
        target.sdbid, "tycho2"
    )
    CatalogService(session_factory, {"tdsc": tdsc}).refresh(target.sdbid, "tdsc")
    output = export_ipac(session_factory, target.sdbid, tmp_path / "optical.txt")
    table = Table.read(output, format="ascii.ipac")
    rows = {(row["Band"], row["Phot"]): row for row in table}
    assert rows[("BT", 8.2)]["exclude"] == 0
    assert rows[("BT", 8.3)]["exclude"] == 1
    assert rows[("VT", 8.0)]["exclude"] == 0
    assert "TDSC component photometry preferred" in rows[("BT", 8.3)]["Note2"]
