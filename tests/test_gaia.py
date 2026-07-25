from __future__ import annotations

from astropy.table import Table
from sqlalchemy import select

from sdb_identity.adapters.gaia import GaiaDr3Adapter
from sdb_identity.catalogs import CatalogQueryContext, CatalogService
from sdb_identity.models import CatalogRun, NormalizedMeasurement, RawCatalogRow
from sdb_identity.providers import Astrometry
from sdb_identity.service import AddRequest, IdentityService
from tests.fakes import FakeGaia, astrometry, gaia_candidate


class FakeVizier:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.TIMEOUT = None

    def query_constraints(self, **constraints):
        self.calls.append(constraints)
        return [self.rows] if self.rows else []


class FakeGaiaTap:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def launch_job_async(self, query, **kwargs):
        self.calls.append((query, kwargs))
        rows = self.rows

        class Job:
            def get_results(self):
                return rows

        return Job()


def gaia_row(**values):
    row = {
        "Source": "123456789",
        "RA_ICRS": 10.0,
        "DE_ICRS": -20.0,
        "e_RA_ICRS": 0.12,
        "e_DE_ICRS": 0.09,
        "PSS": 0.87,
        "Gmag": 8.1,
        "e_Gmag": 0.001,
        "FG": 123456.0,
        "e_FG": 12.0,
        "o_Gmag": 210,
        "BPmag": 8.4,
        "e_BPmag": 0.002,
        "FBP": 65432.0,
        "e_FBP": 15.0,
        "o_BPmag": 24,
        "NBPcont": 1,
        "NBPblend": 2,
        "RPmag": 7.7,
        "e_RPmag": 0.002,
        "FRP": 98765.0,
        "e_FRP": 14.0,
        "o_RPmag": 25,
        "NRPcont": 0,
        "NRPblend": 0,
        "E(BP/RP)": 1.12,
    }
    row.update(values)
    return row


def test_gaia_row_preserves_fluxes_and_normalizes_native_magnitudes():
    candidate = GaiaDr3Adapter.parse_row(gaia_row())
    assert candidate.source_id == "123456789"
    assert [value.band for value in candidate.measurements] == [
        "GAIA.G", "GAIA.BP", "GAIA.RP",
    ]
    assert [value.value for value in candidate.measurements] == [8.1, 8.4, 7.7]
    assert candidate.payload["FBP"] == 65432.0
    assert "flux:65432 e-/s" in candidate.measurements[1].note1
    assert candidate.measurements[1].quality == "n_obs=24;n_cont=1;n_blend=2"
    assert candidate.measurements[1].blend_state == "blended"
    assert candidate.measurements[1].blend_reason == "provider_flagged"
    assert candidate.measurements[1].excluded is False
    review = candidate.payload["_sdb_review"]
    assert review["fields"] == [{
        "key": "single_star_probability",
        "label": "single-star probability",
        "value": 0.87,
        "unit": None,
        "source_column": "PSS",
    }]
    assert review["position_uncertainty"]["major_arcsec"] == 0.00012


def test_gaia_missing_band_is_not_normalized():
    candidate = GaiaDr3Adapter.parse_row(gaia_row(BPmag=None))
    assert [value.band for value in candidate.measurements] == ["GAIA.G", "GAIA.RP"]


def test_gaia_query_uses_established_source_id_not_a_cone():
    client = FakeVizier([gaia_row()])
    adapter = GaiaDr3Adapter()
    adapter.create_client = lambda: client
    context = CatalogQueryContext(
        1,
        "target",
        Astrometry(10, -20, 2016, source="input"),
        ("HD 1", "Gaia DR3 123456789"),
    )
    candidates = adapter.query(context)
    assert [value.source_id for value in candidates] == ["123456789"]
    assert client.calls == [{"catalog": "I/355/gaiadr3", "Source": "123456789"}]


def test_gaia_query_prefers_identifier_over_conflicting_astrometric_source_id():
    adapter = GaiaDr3Adapter()
    context = CatalogQueryContext(
        1,
        "target",
        Astrometry(10, -20, 2016, source="gaia_dr3", source_id="999999999"),
        ("HD 1", "Gaia DR3 123456789", "Gaia DR3 999999999"),
    )

    assert adapter.source_id(context) == "123456789"


def test_gaia_query_without_an_established_source_id_is_no_match():
    client = FakeVizier([gaia_row()])
    adapter = GaiaDr3Adapter()
    adapter.create_client = lambda: client
    context = CatalogQueryContext(
        1, "target", Astrometry(10, -20, 2016), ("HD 1",)
    )
    assert adapter.query(context) == []
    assert client.calls == []


def test_gaia_bulk_query_uploads_ids_and_maps_rows_to_targets():
    row = gaia_row(input_target_id=7, bp_rp_excess_factor=1.12)
    row.pop("E(BP/RP)")
    table = Table(rows=[tuple(row.values())], names=tuple(row.keys()))
    client = FakeGaiaTap(table)
    adapter = GaiaDr3Adapter()
    adapter.create_bulk_client = lambda: client
    contexts = (
        CatalogQueryContext(
            7, "with-gaia", Astrometry(10, -20, 2016),
            ("Gaia DR3 123456789",),
        ),
        CatalogQueryContext(
            8, "without-gaia", Astrometry(20, -30, 2016), ("HD 2",),
        ),
    )

    result = adapter.query_many(contexts)

    assert [candidate.source_id for candidate in result[7]] == ["123456789"]
    assert result[8] == []
    query, kwargs = client.calls[0]
    assert "JOIN gaiadr3.gaia_source" in query
    assert "classprob_dsc_combmod_star AS PSS" in query
    assert kwargs["upload_table_name"] == "targets"
    assert list(kwargs["upload_resource"]["input_target_id"]) == [7]


def test_gaia_refresh_reuses_identity_source_and_stores_photometry(session_factory):
    identity_gaia = FakeGaia([
        gaia_candidate(
            "123456789",
            astrometry(10, -20, epoch=2016, source="gaia_dr3"),
        )
    ])
    target = IdentityService(session_factory, gaia=identity_gaia).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    client = FakeVizier([gaia_row()])
    adapter = GaiaDr3Adapter()
    adapter.create_client = lambda: client
    result = CatalogService(session_factory, {"gaia_dr3": adapter}).refresh(
        target.sdbid, "gaia_dr3"
    )
    assert (result.status, result.selected_source_id, result.measurement_count) == (
        "match", "123456789", 3,
    )
    with session_factory() as session:
        run = session.scalar(select(CatalogRun))
        raw = session.scalar(select(RawCatalogRow))
        measurements = list(session.scalars(
            select(NormalizedMeasurement).order_by(NormalizedMeasurement.id)
        ))
        assert run.provider == "gaia_dr3"
        assert raw.score == 1.0
        assert [value.band for value in measurements] == [
            "GAIA.G", "GAIA.BP", "GAIA.RP",
        ]
