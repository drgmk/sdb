from __future__ import annotations

import pytest

from sdb_identity.live_providers import AstroqueryGaia, AstroquerySimbad
from sdb_identity.providers import Astrometry


class FakeSimbadTapClient:
    def __init__(self):
        self.queries = []

    def query_tap(self, query):
        self.queries.append(query)
        if "FROM ident AS i" in query:
            return [
                {
                    "input_id": "HD      1",
                    "main_id": "HD 1",
                    "ra": 10.0,
                    "dec": -20.0,
                    "pmra": 12.0,
                    "pmdec": -3.0,
                    "plx_value": 5.0,
                    "rvz_radvel": 22.0,
                    "oid": 101,
                    "coo_bibcode": "2020A&A...000....1A",
                    "pm_bibcode": "2020A&A...000....2B",
                    "plx_bibcode": "2020A&A...000....3C",
                    "rvz_bibcode": "2020A&A...000....4D",
                },
                {
                    "input_id": "HD 2",
                    "main_id": "HD 2",
                    "ra": 11.0,
                    "dec": -21.0,
                    "pmra": 13.0,
                    "pmdec": -4.0,
                    "plx_value": 6.0,
                    "rvz_radvel": 23.0,
                    "oid": 102,
                    "coo_bibcode": "2021A&A...000....1A",
                    "pm_bibcode": "2021A&A...000....2B",
                    "plx_bibcode": "2021A&A...000....3C",
                    "rvz_bibcode": "2021A&A...000....4D",
                },
            ]
        if "SELECT oidref, id FROM ident" in query:
            return [
                {"oidref": 101, "id": "HD 1"},
                {"oidref": 101, "id": "HIP 1"},
                {"oidref": 102, "id": "HD 2"},
            ]
        raise AssertionError(query)


def test_parse_simbad_row():
    result = AstroquerySimbad.parse_row({
        "main_id": "HD 1",
        "ra": 10.0,
        "dec": -20.0,
        "pmra": 12.0,
        "pmdec": -3.0,
        "plx_value": 5.0,
        "rvz_radvel": 22.0,
        "coo_bibcode": "2020A&A...000....1A",
        "pm_bibcode": "2020A&A...000....2B",
        "plx_bibcode": "2020A&A...000....3C",
        "rvz_bibcode": "2020A&A...000....4D",
        "ids": "HD 1|HIP 2",
    })
    assert result.main_id == "HD 1"
    assert result.identifiers == ("HD 1", "HIP 2")
    assert result.astrometry.radial_velocity_kms == 22.0
    assert result.astrometry.position_bibcode == "2020A&A...000....1A"
    assert result.astrometry.proper_motion_bibcode == "2020A&A...000....2B"
    assert result.astrometry.parallax_bibcode == "2020A&A...000....3C"
    assert result.astrometry.radial_velocity_bibcode == "2020A&A...000....4D"


def test_simbad_resolve_many_groups_rows_and_identifiers():
    client = FakeSimbadTapClient()
    provider = AstroquerySimbad.__new__(AstroquerySimbad)
    provider.client = client
    result = provider.resolve_many(("HD 1", "HD 2", "Missing"))
    assert result["HD 1"].main_id == "HD 1"
    assert result["HD 1"].identifiers == ("HD 1", "HIP 1")
    assert result["HD 2"].astrometry.pm_dec_masyr == -4.0
    assert result["Missing"] is None
    assert len(client.queries) == 2


def test_simbad_search_region_parses_and_sorts_rows():
    class Client:
        def __init__(self):
            self.query = None

        def query_tap(self, query):
            self.query = query
            return [
                {
                    "oid": 2,
                    "main_id": "HD 1B",
                    "ra": 10.001,
                    "dec": -20.0,
                    "otype": "Star",
                    "otypes": "Star|PM*",
                    "otype_label": "Star",
                    "otype_description": "Star",
                    "sp_type": "M3V",
                    "separation_deg": 0.001,
                },
                {
                    "oid": 1,
                    "main_id": "HD 1",
                    "ra": 10.0,
                    "dec": -20.0,
                    "otype": "Star",
                    "otypes": "Star",
                    "otype_label": "Star",
                    "separation_deg": 0.0,
                },
            ]

    provider = AstroquerySimbad.__new__(AstroquerySimbad)
    provider.client = Client()
    result = provider.search_region(
        Astrometry(10.0, -20.0),
        radius_arcsec=60,
        limit=25,
    )

    assert [row.main_id for row in result] == ["HD 1", "HD 1B"]
    assert result[1].separation_arcsec == pytest.approx(3.6)
    assert result[1].object_types == ("Star", "PM*")
    assert result[1].object_type_description == "Star"
    assert result[1].spectral_type == "M3V"
    assert "SELECT TOP 25" in provider.client.query
    assert "0.016666666666666666" in provider.client.query


@pytest.mark.live
def test_live_simbad_resolve_many_matches_single_resolution():
    provider = AstroquerySimbad(timeout_seconds=30)
    single = provider.resolve_name("HD 661")
    bulk = provider.resolve_many(("HD 661", "HD 3405", "Definitely Missing SDB Test Source"))
    assert bulk["HD 661"] is not None
    assert bulk["HD 661"].main_id == single.main_id
    assert bulk["HD 661"].astrometry.ra_deg == pytest.approx(single.astrometry.ra_deg)
    assert bulk["HD 661"].astrometry.dec_deg == pytest.approx(single.astrometry.dec_deg)
    assert "2MASS J00103851-7313278" in bulk["HD 661"].identifiers
    assert bulk["HD 3405"] is not None
    assert bulk["Definitely Missing SDB Test Source"] is None


@pytest.mark.live
def test_live_simbad_resolve_many_accepts_simbad_return_spacing():
    provider = AstroquerySimbad(timeout_seconds=30)
    compact = provider.resolve_many(("HD 661",))
    padded = provider.resolve_many(("HD    661",))
    assert compact["HD 661"].main_id == padded["HD    661"].main_id


def test_parse_gaia_row():
    result = AstroqueryGaia.parse_row({
        "source_id": 123,
        "ra": 10.0,
        "dec": -20.0,
        "ref_epoch": 2016.0,
        "pmra": 12.0,
        "pmdec": -3.0,
        "parallax": 5.0,
        "radial_velocity": 22.0,
    })
    assert result.source_id == "123"
    assert result.astrometry.epoch == 2016.0
    assert result.astrometry.parallax_mas == 5.0
    assert result.astrometry.pm_dec_masyr == -3.0
    assert result.astrometry.position_bibcode == "2023A&A...674A...1G"


def test_parse_gaia_vizier_pmde_column():
    result = AstroqueryGaia.parse_row({
        "Source": 123,
        "RA_ICRS": 10.0,
        "DE_ICRS": -20.0,
        "pmRA": 12.0,
        "pmDE": -3.0,
        "Plx": 5.0,
        "RV": 22.0,
    })
    assert result.astrometry.pm_ra_cosdec_masyr == 12.0
    assert result.astrometry.pm_dec_masyr == -3.0
    assert result.astrometry.proper_motion_available
    assert result.astrometry.proper_motion_bibcode == "2023A&A...674A...1G"


def test_parse_gaia_row_missing_pm_does_not_claim_pm_reference():
    result = AstroqueryGaia.parse_row({
        "Source": 123,
        "RA_ICRS": 10.0,
        "DE_ICRS": -20.0,
        "pmRA": None,
        "pmDE": None,
        "Plx": None,
        "RV": None,
    })

    assert result.astrometry.position_bibcode == "2023A&A...674A...1G"
    assert not result.astrometry.proper_motion_available
    assert result.astrometry.proper_motion_bibcode is None
    assert result.astrometry.parallax_bibcode is None
    assert result.astrometry.radial_velocity_bibcode is None
