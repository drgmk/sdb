from __future__ import annotations

import pytest
import astropy.units as u
from astropy.coordinates import SkyCoord

from sdb_identity.adapters import allwise
from sdb_identity.adapters.allwise import AllWiseAdapter
from sdb_identity.providers import ProviderError
from sdb_identity.catalog_types import CatalogQueryContext
from sdb_identity.providers import Astrometry


def test_allwise_defaults_to_vizier_backend(monkeypatch):
    monkeypatch.delenv(allwise.ALLWISE_BACKEND_ENV, raising=False)
    adapter = AllWiseAdapter()

    class FakeClient:
        TIMEOUT = None

        def __init__(self):
            self.calls = []

        def query_region(self, coordinate, *, radius, catalog):
            self.calls.append((coordinate, radius, catalog))
            return [[{
                "AllWISE": "J004000.00-200000.0",
                "RAJ2000": 10.0,
                "DEJ2000": -20.0,
                "W1mag": 8.1,
                "e_W1mag": 0.02,
                "W2mag": 7.9,
                "e_W2mag": 0.03,
                "qph": "AAAA",
                "ccf": "0000",
            }]]

    client = FakeClient()
    adapter.create_client = lambda: client
    context = CatalogQueryContext(
        1, "sdbid-v3-test", Astrometry(10, -20, 2010.5),
        ("WISEA J004000.00-200000.0",),
    )

    candidates = adapter.query(context)

    assert client.calls[0][2] == "II/328/allwise"
    assert candidates[0].source_id == "J004000.00-200000.0"
    association = candidates[0].payload["_sdb_association"]
    assert association["query_service"] == "VizieR"
    assert association["query_catalog"] == "II/328/allwise"
    assert association["query_radius_arcsec"] == adapter.review_radius_arcsec
    assert association["acceptance_radius_arcsec"] == adapter.radius_arcsec
    assert association["review_only"] is False
    assert association["identifier_agreement"] is True
    assert [measurement.band for measurement in candidates[0].measurements] == [
        "WISE3P4", "WISE4P6",
    ]


def test_allwise_irsa_backend_can_be_selected_with_env(monkeypatch):
    monkeypatch.setenv(allwise.ALLWISE_BACKEND_ENV, "irsa")
    adapter = AllWiseAdapter()

    def fake_query_region(coordinate, radius_arcsec):
        return [{
            "designation": "J004000.00-200000.0",
            "ra": 10.0,
            "dec": -20.0,
            "w1mpro": 8.1,
            "w1sigmpro": 0.02,
            "ph_qual": "AAAA",
            "cc_flags": "0000",
        }]

    adapter._query_region = fake_query_region
    context = CatalogQueryContext(
        1, "sdbid-v3-test", Astrometry(10, -20, 2010.5),
        ("WISEA J004000.00-200000.0",),
    )

    candidates = adapter.query(context)

    association = candidates[0].payload["_sdb_association"]
    assert association["query_service"] == "IRSA"
    assert association["query_catalog"] == "allwise_p3as_psd"
    assert association["query_radius_arcsec"] == adapter.review_radius_arcsec
    assert association["acceptance_radius_arcsec"] == adapter.radius_arcsec
    assert association["review_only"] is False
    assert association["identifier_agreement"] is True


def test_allwise_backend_argument_overrides_env(monkeypatch):
    monkeypatch.setenv(allwise.ALLWISE_BACKEND_ENV, "irsa")
    assert AllWiseAdapter(backend="vizier").selected_backend() == "vizier"


def test_allwise_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv(allwise.ALLWISE_BACKEND_ENV, "tap")
    with pytest.raises(ProviderError, match="unsupported AllWISE backend"):
        AllWiseAdapter().selected_backend()


def test_allwise_irsa_client_is_per_query_and_has_bounded_timeout(monkeypatch):
    adapter = AllWiseAdapter()
    observed = {}

    def fake_query(self, coordinate, **kwargs):
        observed["session"] = self._session
        observed["kwargs"] = kwargs
        return []

    monkeypatch.setattr(
        "sdb_identity.adapters.allwise.IrsaClass.query_region", fake_query
    )
    adapter._query_region(SkyCoord(10 * u.deg, -20 * u.deg), 3.5)

    assert observed["session"].timeout_seconds == 30.0
    assert observed["kwargs"]["catalog"] == "allwise_p3as_psd"
    assert observed["kwargs"]["radius"].to_value(u.arcsec) == 3.5


def test_allwise_row_normalizes_legacy_bands_and_flags():
    row = AllWiseAdapter.parse_row({
        "AllWISE": "J004000.00-200000.0",
        "RAJ2000": 10.0,
        "DEJ2000": -20.0,
        "W1mag": 8.1,
        "e_W1mag": 0.02,
        "W2mag": 7.9,
        "e_W2mag": 0.03,
        "W3mag": 7.7,
        "e_W3mag": 0.04,
        "W4mag": 7.5,
        "e_W4mag": 0.05,
        "qph": "AUZA",
        "ccf": "0H00",
        "var": "12nn",
    })
    assert row.source_id == "J004000.00-200000.0"
    assert row.epoch == 2010.5
    assert [value.band for value in row.measurements] == [
        "WISE3P4", "WISE4P6", "WISE12", "WISE22",
    ]
    assert [value.systematic_error for value in row.measurements] == [
        0.024, 0.028, 0.045, 0.057,
    ]
    assert [value.upper_limit for value in row.measurements] == [False, True, False, False]
    assert [value.excluded for value in row.measurements] == [False, True, True, False]


def test_allwise_adapter_owns_review_column_meanings_and_units():
    row = AllWiseAdapter.parse_row({
        "AllWISE": "J004000.00-200000.0",
        "RAJ2000": 10.0,
        "DEJ2000": -20.0,
        "_r": 0.12,
        "eeMaj": 0.4,
        "eeMin": 0.2,
        "eePA": 0.0,
        "nb": 2,
        "na": 1,
        "qph": "AAAA",
        "ccf": "0000",
    })

    review = row.payload["_sdb_review"]
    assert review["neighbourhood_flags"] == {
        "active_deblend": 1,
        "simultaneous_psf_components": 2,
    }
    assert review["position_uncertainty"] == {
        "kind": "error_ellipse",
        "major_arcsec": 0.4,
        "minor_arcsec": 0.2,
        "position_angle_deg": 0.0,
        "source_columns": ["eeMaj", "eeMin", "eePA"],
    }
    assert all(field["source_column"] != "_r" for field in review["fields"])
    assert all(field["source_column"] != "prox" for field in review["fields"])


def test_allwise_irsa_review_metadata_uses_coordinate_errors():
    row = AllWiseAdapter.parse_row({
        "designation": "J004000.00-200000.0",
        "ra": 10.0,
        "dec": -20.0,
        "sigra": 0.31,
        "sigdec": 0.27,
        "nb": 1,
        "na": 0,
        "ph_qual": "AAAA",
        "cc_flags": "0000",
        "ext_flg": 0,
    })

    uncertainty = row.payload["_sdb_review"]["position_uncertainty"]
    assert uncertainty == {
        "kind": "coordinate_errors",
        "major_arcsec": 0.31,
        "minor_arcsec": 0.27,
        "source_columns": ["sigra", "sigdec"],
    }


def test_allwise_vizier_apparent_motion_is_retained_without_pm_keys():
    row = AllWiseAdapter.parse_row({
        "AllWISE": "J004000.00-200000.0",
        "RAJ2000": 10.0,
        "DEJ2000": -20.0,
        "pmRA": 12.0,
        "e_pmRA": 3.0,
        "pmDE": -7.0,
        "e_pmDE": 4.0,
        "qpm": "A",
    })

    assert [(value.key, value.uncertainty, value.quality) for value in row.attributes] == [
        ("apparent_motion_ra_cosdec", 3.0, "A"),
        ("apparent_motion_dec", 4.0, "A"),
    ]
    assert all("not proper motion" in value.note for value in row.attributes)


def test_allwise_bright_limits_and_missing_magnitudes():
    row = AllWiseAdapter.parse_row({
        "AllWISE": "J004000.00-200000.0",
        "RAJ2000": 10.0,
        "DEJ2000": -20.0,
        "W1mag": 4.4,
        "e_W1mag": 0.02,
        "W2mag": 3.9,
        "e_W2mag": 0.03,
        "W3mag": -0.1,
        "e_W3mag": 0.04,
        "W4mag": None,
        "qph": "AAAA",
        "ccf": "0000",
        "var": "0000",
    })
    assert [value.band for value in row.measurements] == ["WISE3P4", "WISE4P6", "WISE12"]
    assert all(value.excluded for value in row.measurements)


def test_allwise_declares_simbad_identifier_evidence():
    adapter = AllWiseAdapter()
    context = CatalogQueryContext(
        1, "sdbid-v3-test", Astrometry(10, -20),
        ("WISEA J004000.00-200000.0",),
    )
    candidate = adapter.parse_row({
        "AllWISE": "J004000.00-200000.0", "RAJ2000": 10.0,
        "DEJ2000": -20.0, "W1mag": 8.0, "qph": "AAAA",
        "ccf": "0000",
    })
    assert adapter.expected_source_ids(context.identifiers) == {
        "J004000.00-200000.0"
    }
    assert adapter.score_candidate(context, candidate, 1.5) == 1.0


def test_allwise_marks_wide_cone_rows_review_only_and_cannot_score_as_match():
    adapter = AllWiseAdapter()
    context = CatalogQueryContext(
        1, "sdbid-v3-test", Astrometry(10, -20, 2010.5),
        ("WISEA J004000.70-200000.0",),
    )
    candidate = adapter.parse_row({
        "AllWISE": "J004000.70-200000.0",
        "RAJ2000": 10.0029166667,
        "DEJ2000": -20.0,
        "W1mag": 8.0,
        "qph": "AAAA",
        "ccf": "0000",
    })

    annotated = adapter.annotate_candidate(
        candidate,
        query_service="VizieR",
        query_catalog=adapter.release,
        query_radius_arcsec=adapter.query_radius(context),
        acceptance_radius_arcsec=adapter.acceptance_radius(context),
        context=context,
        expected=adapter.expected_source_ids(context.identifiers),
    )
    association = annotated.payload["_sdb_association"]

    assert association["query_radius_arcsec"] == adapter.review_radius_arcsec
    assert association["acceptance_radius_arcsec"] == adapter.radius_arcsec
    assert association["identifier_agreement"] is True
    assert association["candidate_separation_arcsec"] > adapter.radius_arcsec
    assert association["review_only"] is True
    assert adapter.score_candidate(context, annotated, association["candidate_separation_arcsec"]) == 0.0


def test_allwise_cone_covers_half_year_of_target_motion():
    adapter = AllWiseAdapter()
    context = CatalogQueryContext(
        1,
        "sdbid-v3-test",
        Astrometry(10, -20, 2010.5, 3000, 4000),
    )
    assert adapter.query_epoch == 2010.5
    assert adapter.acceptance_radius(context) == 4.5
    assert adapter.query_radius(context) == adapter.review_radius_arcsec


def test_allwise_cone_uses_review_radius_without_proper_motion():
    adapter = AllWiseAdapter()
    context = CatalogQueryContext(1, "sdbid-v3-test", Astrometry(10, -20, 2010.5))
    assert adapter.acceptance_radius(context) == adapter.radius_arcsec
    assert adapter.query_radius(context) == adapter.review_radius_arcsec


def test_allwise_irsa_row_uses_profile_photometry_and_retains_motion():
    row = AllWiseAdapter.parse_row({
        "designation": "J000529.35-372151.0",
        "ra": 1.3723131,
        "dec": -37.3641778,
        "w1mpro": 4.403,
        "w1sigmpro": 0.295,
        "w1mag": 7.805,
        "w1sigm": 0.005,
        "w2mpro": 4.038,
        "w2sigmpro": 0.173,
        "w2mag": 7.088,
        "w2sigm": 0.005,
        "ph_qual": "BBAA",
        "cc_flags": "0000",
        "var_flg": "nnnn",
        "pmra": -471,
        "sigpmra": 77,
        "pmdec": -2662,
        "sigpmdec": 86,
        "pmcode": "1N007",
    })
    assert row.source_id == "J000529.35-372151.0"
    assert [value.value for value in row.measurements] == [4.403, 4.038]
    assert [value.key for value in row.attributes] == [
        "apparent_motion_ra_cosdec", "apparent_motion_dec",
    ]
    assert row.attributes[0].quality == "1N007"
