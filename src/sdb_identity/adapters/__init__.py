"""Catalog adapter implementations.

Display, identifier, and component policy lives in :mod:`catalog_policy` so
using those small helpers does not import every adapter implementation.
"""

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
]
