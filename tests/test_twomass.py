from __future__ import annotations

from sdb_identity.catalogs.adapters.twomass import TwoMassAdapter
from sdb_identity.astrometry import propagate_to_epoch
from sdb_identity.catalogs.types import CatalogQueryContext
from sdb_identity.providers import Astrometry


def test_twomass_row_parsing_and_legacy_band_names():
    rows = TwoMassAdapter.parse_row({
        "_2MASS": "00400000-2000000",
        "RAJ2000": 10.0,
        "DEJ2000": -20.0,
        "Jmag": 7.1,
        "e_Jmag": 0.02,
        "Hmag": 6.9,
        "e_Hmag": 0.03,
        "Kmag": 6.8,
        "e_Kmag": 0.04,
        "Qflg": "ABC",
        "Rflg": "120",
        "Cflg": "00p",
        "Date": "1999-03-01",
    })
    assert rows.source_id == "00400000-2000000"
    assert rows.epoch > 1999.0
    assert [value.band for value in rows.measurements] == ["2MR1J", "2MR2H", "2MKS"]
    assert [value.excluded for value in rows.measurements] == [False, False, True]


def test_twomass_missing_magnitude_is_not_normalized():
    row = TwoMassAdapter.parse_row({
        "2MASS": "00400000-2000000",
        "RAJ2000": 10.0,
        "DEJ2000": -20.0,
        "Jmag": None,
        "Hmag": 6.9,
        "e_Hmag": 0.03,
        "Qflg": "AAA",
        "Rflg": "000",
        "Cflg": "000",
    })
    assert [value.band for value in row.measurements] == ["2MH"]


def test_twomass_declares_simbad_identifier_evidence():
    adapter = TwoMassAdapter()
    context = CatalogQueryContext(
        1, "sdbid-v3-test", Astrometry(10, -20),
        ("HD 1", "2MASS J00400000-2000000"),
    )
    candidate = adapter.parse_row({
        "2MASS": "00400000-2000000", "RAJ2000": 10.0,
        "DEJ2000": -20.0, "Jmag": 7.1, "Qflg": "AAA",
        "Rflg": "111", "Cflg": "000",
    })
    assert adapter.expected_source_ids(context.identifiers) == {
        "00400000-2000000"
    }
    assert adapter.score_candidate(context, candidate, 1.5) == 1.0


def test_twomass_query_cone_covers_full_survey_motion_window():
    adapter = TwoMassAdapter()
    context = CatalogQueryContext(
        1,
        "sdbid-v3-test",
        Astrometry(10, -20, adapter.query_epoch, 3000, 4000),
    )
    assert adapter.acceptance_radius(context) == 11.6
    # The review cone is deliberately wider so nearby non-accepted sources
    # remain available as context.
    assert adapter.query_radius(context) == 15.0


def test_twomass_candidate_separation_uses_row_observation_epoch():
    adapter = TwoMassAdapter()
    target = Astrometry(10, -20, 2000, 3000, 4000)
    at_date = propagate_to_epoch(target, 1997.5)
    context = CatalogQueryContext(1, "sdbid-v3-test", target)
    candidate = adapter.parse_row({
        "2MASS": "00400000-2000000",
        "RAJ2000": at_date.ra_deg,
        "DEJ2000": at_date.dec_deg,
        "Date": "1997-07-02",
        "Jmag": 7.1,
        "Qflg": "AAA",
        "Rflg": "111",
        "Cflg": "000",
    })
    assert adapter.candidate_separation(context, candidate) < 0.02


def test_twomass_irsa_row_uses_combined_errors_and_observation_date():
    candidate = TwoMassAdapter.parse_row({
        "designation": "00052423-3721257",
        "ra": 1.350976,
        "dec": -37.357159,
        "xdate": "1999-08-02",
        "j_m": 5.328,
        "j_msigcom": 0.019,
        "h_m": 4.828,
        "h_msigcom": 0.076,
        "k_m": 4.523,
        "k_msigcom": 0.017,
        "ph_qual": "AEE",
        "rd_flg": "111",
        "cc_flg": "000",
    })
    assert candidate.source_id == "00052423-3721257"
    assert candidate.epoch > 1999.5
    assert [value.error for value in candidate.measurements] == [0.019, 0.076, 0.017]
