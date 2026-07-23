"""Catalog adapters for querying and normalizing external data."""

from .allwise import AllWiseAdapter
from .gaia import GaiaDr3Adapter
from .reference import (
    GasparSnapshotAdapter,
    Hip2SnapshotAdapter,
    IrasFscSnapshotAdapter,
    IrasPscSnapshotAdapter,
    SnapshotCatalogAdapter,
    TdscSnapshotAdapter,
    V70ASnapshotAdapter,
    snapshot_adapter,
)
from .twomass import TwoMassAdapter
from .tycho2 import Tycho2Adapter
from .vizier import BandDefinition, VizierConeAdapter


_IDENTIFIER_ADAPTERS = {
    "2mass": TwoMassAdapter,
    "allwise": AllWiseAdapter,
    "gaia_dr3": GaiaDr3Adapter,
    "tycho2": Tycho2Adapter,
}


def catalog_source_id_matches_identifiers(
    provider: str,
    source_id: str,
    identifiers: tuple[str, ...],
) -> bool:
    """Apply the provider's own source-ID convention to SIMBAD aliases."""
    adapter = _IDENTIFIER_ADAPTERS.get(provider)
    if adapter is not None:
        return adapter.source_id_matches_identifiers(source_id, identifiers)
    normalized = " ".join(str(source_id).upper().split())
    return normalized in {" ".join(value.upper().split()) for value in identifiers}


def catalog_source_display_name(provider: str, source_id: str) -> str:
    """Return the adapter-owned, human-readable catalog source name."""
    adapter = _IDENTIFIER_ADAPTERS.get(provider)
    formatter = None if adapter is None else getattr(adapter, "display_source_id", None)
    if formatter is not None:
        return str(formatter(source_id))
    return str(source_id).strip()

__all__ = [
    "AllWiseAdapter",
    "BandDefinition",
    "GasparSnapshotAdapter",
    "GaiaDr3Adapter",
    "Hip2SnapshotAdapter",
    "IrasFscSnapshotAdapter",
    "IrasPscSnapshotAdapter",
    "SnapshotCatalogAdapter",
    "TdscSnapshotAdapter",
    "TwoMassAdapter",
    "Tycho2Adapter",
    "V70ASnapshotAdapter",
    "VizierConeAdapter",
    "snapshot_adapter",
    "catalog_source_id_matches_identifiers",
    "catalog_source_display_name",
]
