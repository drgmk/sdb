from __future__ import annotations

import pytest

from sdb_identity.catalogs import CatalogQueryContext
from sdb_identity.providers import Astrometry, ProviderError
from sdb_identity.adapters import twomass
from sdb_identity.adapters.twomass import TwoMassAdapter


def context() -> CatalogQueryContext:
    return CatalogQueryContext(
        target_id=1,
        sdbid="sdbid-v3-004000.00-200000.0",
        astrometry=Astrometry(10.0, -20.0, epoch=1999.3),
        identifiers=("2MASS J00400000-2000000",),
    )


def vizier_row() -> dict[str, object]:
    return {
        "2MASS": "00400000-2000000",
        "RAJ2000": 10.0,
        "DEJ2000": -20.0,
        "Date": "1998-10-01",
        "Qflg": "AAA",
        "Rflg": "222",
        "Cflg": "000",
        "prox": 4.2,
        "errMaj": 0.12,
        "errMin": 0.08,
        "errPA": 20.0,
        "Jmag": 7.1,
        "e_Jmag": 0.02,
        "Hmag": 6.9,
        "e_Hmag": 0.03,
        "Kmag": 6.8,
        "e_Kmag": 0.04,
    }


def irsa_row() -> dict[str, object]:
    return {
        "designation": "00400000-2000000",
        "ra": 10.0,
        "dec": -20.0,
        "xdate": "1998-10-01",
        "ph_qual": "AAA",
        "rd_flg": "222",
        "cc_flg": "000",
        "j_m": 7.1,
        "j_msigcom": 0.02,
        "h_m": 6.9,
        "h_msigcom": 0.03,
        "k_m": 6.8,
        "k_msigcom": 0.04,
    }


def test_twomass_defaults_to_vizier_backend(monkeypatch):
    monkeypatch.delenv(twomass.TWOMASS_BACKEND_ENV, raising=False)
    adapter = TwoMassAdapter()

    class FakeClient:
        TIMEOUT = None

        def __init__(self):
            self.calls = []

        def query_region(self, coordinate, *, radius, catalog):
            self.calls.append((coordinate, radius, catalog))
            return [[vizier_row()]]

    client = FakeClient()
    adapter.create_client = lambda: client

    candidates = adapter.query(context())

    assert client.calls[0][2] == "II/246/out"
    assert candidates[0].source_id == "00400000-2000000"
    association = candidates[0].payload["_sdb_association"]
    assert association["query_service"] == "VizieR"
    assert association["query_catalog"] == "II/246/out"
    assert association["identifier_agreement"] is True
    assert [measurement.band for measurement in candidates[0].measurements] == [
        "2MR2J", "2MR2H", "2MR2KS",
    ]
    review = candidates[0].payload["_sdb_review"]
    assert review["neighbourhood_flags"] == {"nearest_source_arcsec": 4.2}
    assert review["position_uncertainty"]["major_arcsec"] == 0.12


def test_twomass_irsa_backend_can_be_selected_with_env(monkeypatch):
    monkeypatch.setenv(twomass.TWOMASS_BACKEND_ENV, "irsa")
    seen = {}

    class FakeIrsa:
        @staticmethod
        def query_region(coordinate, *, catalog, spatial, radius):
            seen.update({"catalog": catalog, "spatial": spatial, "radius": radius})
            return [irsa_row()]

    monkeypatch.setattr(twomass, "Irsa", FakeIrsa)

    candidates = TwoMassAdapter().query(context())

    assert seen["catalog"] == "fp_psc"
    assert seen["spatial"] == "Cone"
    association = candidates[0].payload["_sdb_association"]
    assert association["query_service"] == "IRSA"
    assert association["query_catalog"] == "fp_psc"
    assert association["identifier_agreement"] is True


def test_twomass_backend_argument_overrides_env(monkeypatch):
    monkeypatch.setenv(twomass.TWOMASS_BACKEND_ENV, "irsa")
    assert TwoMassAdapter(backend="vizier").selected_backend() == "vizier"


def test_twomass_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv(twomass.TWOMASS_BACKEND_ENV, "tap")
    with pytest.raises(ProviderError, match="unsupported 2MASS backend"):
        TwoMassAdapter().selected_backend()
