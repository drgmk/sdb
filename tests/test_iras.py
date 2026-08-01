from __future__ import annotations

from astropy.table import Table
import pytest
from sqlalchemy import select

from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.catalog_types import CatalogCandidate, MeasurementValue
from sdb_identity.cli import main
from sdb_identity.export import export_ipac
from sdb_identity.models.catalogs import (
    IrasBandSelection,
    IrasDetectionFamily,
    NormalizedMeasurement,
)
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog


def _candidate(source_id, provider, *, ra=10.0, quality="3", upper_limit=False):
    return CatalogCandidate(
        source_id=source_id,
        ra_deg=ra,
        dec_deg=0.0,
        epoch=1983.5,
        payload={
            "IRAS": source_id,
            "RA1950": "00 40 00",
            "DE1950": "+00 00 00",
            "Major": 10,
            "Minor": 4,
            "PosAng": 0,
        },
        measurements=(MeasurementValue(
            band="IRAS12",
            value=1.0 if provider == "iras_psc" else 2.0,
            error=0.1,
            unit="Jy",
            quality=quality,
            upper_limit=upper_limit,
            bibcode="test",
        ),),
    )


def test_iras_family_selects_detection_quality_then_psc_and_export_retains_alternate(
    session_factory, tmp_path
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    psc = FakeCatalog(
        [_candidate("00000+0000", "iras_psc", quality="3")],
        name="iras_psc",
    )
    fsc = FakeCatalog(
        [_candidate("F00000+0000", "iras_fsc", quality="3")],
        name="iras_fsc",
    )
    CatalogAcquisitionService(session_factory, {"iras_psc": psc}).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {"iras_fsc": fsc}).refresh(target.sdbid, "iras_fsc")

    with session_factory() as session:
        family = session.scalar(select(IrasDetectionFamily))
        selection = session.scalar(select(IrasBandSelection))
        assert family.status == "associated"
        assert family.normalized_separation == pytest.approx(0.0, abs=1e-10)
        assert selection.band == "IRAS12"
        assert selection.selected_measurement_id != selection.alternate_measurement_id

    output = export_ipac(session_factory, target.sdbid, tmp_path / "iras.txt")
    table = Table.read(output, format="ascii.ipac")
    by_phot = {float(row["Phot"]): row for row in table}
    assert int(by_phot[1.0]["exclude"]) == 0
    assert int(by_phot[2.0]["exclude"]) == 1
    assert "alternate PSC/FSC" in by_phot[2.0]["Note2"]


def test_iras_family_prefers_detection_over_higher_catalog_precedence(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    psc = FakeCatalog(
        [_candidate("00000+0000", "iras_psc", quality="1", upper_limit=True)],
        name="iras_psc",
    )
    fsc = FakeCatalog(
        [_candidate("F00000+0000", "iras_fsc", quality="2")],
        name="iras_fsc",
    )
    CatalogAcquisitionService(session_factory, {"iras_psc": psc}).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {"iras_fsc": fsc}).refresh(target.sdbid, "iras_fsc")
    with session_factory() as session:
        selection = session.scalar(select(IrasBandSelection))
        assert selection is not None
        selected = session.get(NormalizedMeasurement, selection.selected_measurement_id)
        assert selected.provider == "iras_fsc"
        assert selection.method == "quality_then_psc"


def test_iras_family_does_not_merge_ellipse_inconsistent_rows(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    offset = 1.5 / 3600.0
    psc_candidate = _candidate("00000+0000", "iras_psc", ra=10 - offset)
    fsc_candidate = _candidate("F00000+0000", "iras_fsc", ra=10 + offset)
    psc_candidate.payload["Major"] = psc_candidate.payload["Minor"] = 0.5
    fsc_candidate.payload["Major"] = fsc_candidate.payload["Minor"] = 0.5
    CatalogAcquisitionService(
        session_factory, {"iras_psc": FakeCatalog([psc_candidate], name="iras_psc")}
    ).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(
        session_factory, {"iras_fsc": FakeCatalog([fsc_candidate], name="iras_fsc")}
    ).refresh(target.sdbid, "iras_fsc")
    with session_factory() as session:
        family = session.scalar(select(IrasDetectionFamily))
        assert family.status == "review"
        assert session.scalar(select(IrasBandSelection)) is None


def test_iras_family_review_cli_shows_sources_and_band_decisions(
    session_factory, db_path, capsys
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    CatalogAcquisitionService(session_factory, {
        "iras_psc": FakeCatalog(
            [_candidate("00000+0000", "iras_psc")], name="iras_psc"
        )
    }).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {
        "iras_fsc": FakeCatalog(
            [_candidate("F00000+0000", "iras_fsc")], name="iras_fsc"
        )
    }).refresh(target.sdbid, "iras_fsc")
    assert main([
        "--database", str(db_path), "review", "iras-families", "--all"
    ]) == 0
    value = __import__("json").loads(capsys.readouterr().out)
    assert value["status"] == "associated"
    assert value["psc_source_id"] == "00000+0000"
    assert value["fsc_source_id"] == "F00000+0000"
    assert value["band_selections"][0]["band"] == "IRAS12"
