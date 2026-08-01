from __future__ import annotations

from astroquery.vizier import Vizier

from sdb_identity.catalogs.adapters.gaia import GaiaDr3Adapter
from sdb_identity.astroquery_config import (
    SIMBAD_SERVER_ENV,
    VIZIER_SERVER_ENV,
    configure_vizier_class,
    configured_simbad_client,
)


class FakeSimbadClient:
    server = "default"


class FakeVizierClass:
    VIZIER_SERVER = "default"

    class CONF:
        server = "default"


def test_simbad_server_env_is_applied_to_client(monkeypatch):
    monkeypatch.setenv(SIMBAD_SERVER_ENV, "simbad.harvard.edu")

    client = configured_simbad_client(FakeSimbadClient())

    assert client.server == "simbad.harvard.edu"


def test_vizier_server_env_is_applied_to_supported_class_attrs(monkeypatch):
    monkeypatch.setenv(VIZIER_SERVER_ENV, "vizier.nao.ac.jp")

    configure_vizier_class(FakeVizierClass)

    assert FakeVizierClass.VIZIER_SERVER == "vizier.nao.ac.jp"
    assert FakeVizierClass.CONF.server == "vizier.nao.ac.jp"


def test_gaia_vizier_client_factory_applies_vizier_server_env(monkeypatch):
    monkeypatch.setenv(VIZIER_SERVER_ENV, "vizier.nao.ac.jp")
    monkeypatch.setattr(Vizier, "VIZIER_SERVER", "vizier.cds.unistra.fr")

    GaiaDr3Adapter().create_client()

    assert Vizier.VIZIER_SERVER == "vizier.nao.ac.jp"

