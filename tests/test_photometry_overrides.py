from __future__ import annotations

from astropy.table import Table

from sdb_identity.catalogs import CatalogService
from sdb_identity.export import export_ipac
from sdb_identity.photometry import (
    list_photometry_association_decisions,
    list_photometry_overrides,
    photometry_review_queue,
    review_photometry_associations,
    set_photometry_association_decision,
    set_photometry_override,
)
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement


def allwise_catalog(value=8.1):
    return FakeCatalog(
        [candidate("J004000.00-200000.0", measurements=[measurement("WISE3P4", value)])],
        name="allwise",
        release="fake-allwise",
        query_epoch=2010.3,
    )


def test_manual_exclusion_survives_refresh_and_latest_override_wins(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    service = CatalogService(session_factory, {"allwise": allwise_catalog()})
    service.refresh(target.sdbid, "allwise")

    first = set_photometry_override(
        session_factory,
        target.sdbid,
        provider="allwise",
        band="WISE3P4",
        excluded=True,
        actor="grant",
        reason="blended source",
    )
    service.adapters["allwise"] = allwise_catalog(8.2)
    service.refresh(target.sdbid, "allwise")
    output = export_ipac(session_factory, target.sdbid, tmp_path / "excluded.txt")
    table = Table.read(output, format="ascii.ipac")
    assert list(table["Phot"]) == [8.2]
    assert list(table["exclude"]) == [1]
    assert "blended source" in table["Note2"][0]

    second = set_photometry_override(
        session_factory,
        target.sdbid,
        provider="allwise",
        band="WISE3P4",
        excluded=False,
        actor="grant",
        reason="resolved after review",
    )
    output = export_ipac(session_factory, target.sdbid, tmp_path / "included.txt")
    table = Table.read(output, format="ascii.ipac")
    assert list(table["exclude"]) == [0]
    assert [value.id for value in list_photometry_overrides(session_factory, target.sdbid)] == [
        first.id, second.id,
    ]



def test_photometry_association_decision_is_append_only_and_reviewable(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    service = CatalogService(session_factory, {"allwise": allwise_catalog()})
    service.refresh(target.sdbid, "allwise")

    review = review_photometry_associations(session_factory, target.sdbid)
    assert len(review) == 1
    assert review[0].provider == "allwise"
    assert review[0].band == "WISE3P4"
    assert review[0].current_decision_scope is None

    first = set_photometry_association_decision(
        session_factory,
        target.sdbid,
        provider="allwise",
        source_id="J004000.00-200000.0",
        band="WISE3P4",
        scope="blended",
        actor="grant",
        reason="WISE beam includes both components",
    )
    second = set_photometry_association_decision(
        session_factory,
        target.sdbid,
        provider="allwise",
        source_id="J004000.00-200000.0",
        band="WISE3P4",
        scope="component",
        actor="grant",
        reason="resolved on inspection",
    )

    decisions = list_photometry_association_decisions(session_factory, target.sdbid)
    review = review_photometry_associations(session_factory, target.sdbid)

    assert [value.id for value in decisions] == [first.id, second.id]
    assert review[0].current_decision_scope == "component"
    assert review[0].current_decision_id == second.id
    assert review[0].current_decision_reason == "resolved on inspection"


def test_photometry_review_queue_reports_only_rows_needing_attention(session_factory):
    clean = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    risky = IdentityService(session_factory).add(AddRequest(ra_deg=11, dec_deg=-20))
    CatalogService(session_factory, {
        "allwise": FakeCatalog([candidate(
            "clean-wise", measurements=[measurement("WISE3P4", 8.1)]
        )], name="allwise", release="fake-allwise"),
    }).refresh(clean.sdbid, "allwise")
    CatalogService(session_factory, {
        "allwise": FakeCatalog([candidate(
            "risky-wise", ra=11, dec=-20, measurements=[measurement("WISE3P4", 8.2)]
        )], name="allwise", release="fake-allwise"),
    }).refresh(risky.sdbid, "allwise")
    set_photometry_association_decision(
        session_factory, risky.sdbid, provider="allwise", source_id="risky-wise",
        band="WISE3P4", scope="reject", actor="grant", reason="wrong component",
    )

    rows = photometry_review_queue(session_factory, [clean.sdbid, risky.sdbid])

    assert [row["sdbid"] for row in rows] == [risky.sdbid, clean.sdbid]
    assert rows[0]["signal"] == "association rejected; export excludes"
    assert rows[0]["priority"] == "medium"
    assert rows[0]["current_decision"] == "reject"
    assert rows[1]["signal"] == "no photometry review item"
    assert rows[1]["priority"] == "none"
