"""Construct catalog services consistently for CLI and local review actions."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from .catalogs import CatalogService


REMOTE_CATALOG_PROVIDERS = ("2mass", "allwise", "gaia_dr3", "tycho2")


def catalog_service_for_provider(
    sessions: sessionmaker[Session],
    provider: str,
    *,
    reference_database: str | Path,
    offline: bool,
    action: str = "accept_candidate",
) -> CatalogService:
    if provider in _reference_providers():
        from .reference import ReferenceStore, snapshot_adapter

        adapter = snapshot_adapter(provider, ReferenceStore(reference_database))
    elif offline and action == "retry":
        raise ValueError(
            f"{provider} retry is unavailable while the review server is offline"
        )
    elif provider == "2mass":
        from .adapters.twomass import TwoMassAdapter

        adapter = TwoMassAdapter()
    elif provider == "allwise":
        from .adapters.allwise import AllWiseAdapter

        adapter = AllWiseAdapter()
    elif provider == "gaia_dr3":
        from .adapters.gaia import GaiaDr3Adapter

        adapter = GaiaDr3Adapter()
    elif provider == "tycho2":
        from .adapters.tycho2 import Tycho2Adapter

        adapter = Tycho2Adapter()
    else:
        raise ValueError(f"catalog adapter is unavailable: {provider}")
    return CatalogService(sessions, {provider: adapter})


def _reference_providers() -> tuple[str, ...]:
    from .reference_definitions import SNAPSHOT_CATALOGS

    return tuple(SNAPSHOT_CATALOGS)
