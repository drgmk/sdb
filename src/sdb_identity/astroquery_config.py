from __future__ import annotations

import os
from typing import Any


SIMBAD_SERVER_ENV = "SDB_SIMBAD_SERVER"
VIZIER_SERVER_ENV = "SDB_VIZIER_SERVER"


def configured_simbad_client(client: Any):
    """Apply SDB astroquery SIMBAD environment settings to a client instance."""

    server = os.environ.get(SIMBAD_SERVER_ENV)
    if server:
        if hasattr(client, "server"):
            client.server = server
        elif hasattr(client, "SIMBAD_MIRROR"):
            client.SIMBAD_MIRROR = server
    return client


def configure_vizier_class(vizier_class: Any) -> None:
    """Apply SDB astroquery VizieR environment settings to the Vizier class."""

    server = os.environ.get(VIZIER_SERVER_ENV)
    if not server:
        return
    if hasattr(vizier_class, "VIZIER_SERVER"):
        vizier_class.VIZIER_SERVER = server
    conf = getattr(vizier_class, "CONF", None) or getattr(vizier_class, "Conf", None)
    if conf is not None and hasattr(conf, "server"):
        conf.server = server

