"""Shared snapshot client protocol and the default VizieR client.

Reference-catalog and hierarchy ingestion both fetch full VizieR catalogs plus
their ReadMe, and both accept an injected client so tests can supply fakes.
This module holds the single client protocol and the real VizieR implementation
they share.
"""

from __future__ import annotations

import urllib.request
from typing import Protocol

from .astroquery_config import configure_vizier_class


class SnapshotClient(Protocol):
    def fetch_tables(self, catalog: str): ...
    def fetch_readme(self, catalog: str) -> str: ...
    def source_url(self, catalog: str) -> str: ...


class VizierSnapshotClient:
    """Fetch a full VizieR catalog and its ReadMe via astroquery."""

    provider = "vizier"

    @staticmethod
    def source_url(catalog: str) -> str:
        return f"https://vizier.cds.unistra.fr/viz-bin/VizieR?-source={catalog}"

    def fetch_tables(self, catalog: str):
        from astroquery.vizier import Vizier

        configure_vizier_class(Vizier)
        return Vizier(columns=["**"], row_limit=-1).get_catalogs(catalog)

    def fetch_readme(self, catalog: str) -> str:
        url = f"https://cdsarc.cds.unistra.fr/ftp/{catalog}/ReadMe"
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read().decode("utf-8")
