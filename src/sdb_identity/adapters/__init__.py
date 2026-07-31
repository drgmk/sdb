"""Catalog adapters for querying and normalizing external data."""

import re
from collections.abc import Mapping

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
from ..reference_definitions import SNAPSHOT_CATALOGS
from ..identifiers import normalize_identifier


_IDENTIFIER_ADAPTERS = {
    "2mass": TwoMassAdapter,
    "allwise": AllWiseAdapter,
    "gaia_dr3": GaiaDr3Adapter,
    "tycho2": Tycho2Adapter,
}

# A catalog centroid is not component identity evidence when its native
# detection scale is deliberately system-sized.  These providers can still
# retain an existing accepted system match or match an explicit identifier,
# but proximity alone must not reassign them to a close component.
_SYSTEM_SCALE_POSITION_PROVIDERS = {
    "iras_fsc",
    "iras_psc",
    "submm_obs",
}


def _catalog_identifier_key(value: str) -> str:
    normalized = normalize_identifier(value)
    matched = re.fullmatch(r"(HD|HIP|GL|GJ|LHS)\s*0*(\d+)", normalized)
    if matched is not None:
        return f"{matched.group(1)} {int(matched.group(2))}"
    return normalized


def catalog_candidate_identifiers(
    provider: str,
    source_id: str,
    payload: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    """Return fully-qualified astronomical identifiers for one catalog row.

    Snapshot source IDs are stable database keys and need not themselves be
    astronomical identifiers. Their definitions therefore construct aliases
    from the stored provider payload. Live adapters retain their existing
    source-ID conventions.
    """
    definition = SNAPSHOT_CATALOGS.get(provider)
    if definition is not None and payload is not None:
        return definition.identifiers(dict(payload))
    adapter = _IDENTIFIER_ADAPTERS.get(provider)
    formatter = None if adapter is None else getattr(
        adapter, "display_source_id", None
    )
    if formatter is not None:
        return (str(formatter(source_id)),)
    return (str(source_id).strip(),)


def catalog_source_id_matches_identifiers(
    provider: str,
    source_id: str,
    identifiers: tuple[str, ...],
    *,
    payload: Mapping[str, object] | None = None,
) -> bool:
    """Apply the provider's own source-ID convention to SIMBAD aliases."""
    definition = SNAPSHOT_CATALOGS.get(provider)
    if definition is not None and payload is not None:
        candidate_keys = {
            _catalog_identifier_key(value)
            for value in catalog_candidate_identifiers(provider, source_id, payload)
            if value
        }
        identifier_keys = {
            _catalog_identifier_key(value) for value in identifiers if value
        }
        return bool(candidate_keys & identifier_keys)
    adapter = _IDENTIFIER_ADAPTERS.get(provider)
    if adapter is not None:
        return adapter.source_id_matches_identifiers(source_id, identifiers)
    normalized = _catalog_identifier_key(source_id)
    return normalized in {
        _catalog_identifier_key(value) for value in identifiers if value
    }


def catalog_source_display_name(
    provider: str,
    source_id: str,
    payload: Mapping[str, object] | None = None,
) -> str:
    """Return the adapter-owned, human-readable catalog source name."""
    definition = SNAPSHOT_CATALOGS.get(provider)
    if definition is not None and payload is not None:
        identifiers = catalog_candidate_identifiers(provider, source_id, payload)
        if identifiers:
            return identifiers[0]
    adapter = _IDENTIFIER_ADAPTERS.get(provider)
    formatter = None if adapter is None else getattr(adapter, "display_source_id", None)
    if formatter is not None:
        return str(formatter(source_id))
    return str(source_id).strip()


def catalog_band_wavelength_micron(provider: str, band: str) -> float | None:
    """Return repository-owned wavelength metadata for matrix ordering."""
    value = str(band).strip().upper()
    if provider == "submm_obs" and value.startswith("WAV"):
        try:
            return float(value[3:])
        except ValueError:
            return None

    definition = SNAPSHOT_CATALOGS.get(provider)
    if definition is not None:
        return dict(definition.band_wavelengths_micron).get(value)

    adapter = _IDENTIFIER_ADAPTERS.get(provider)
    wavelengths = () if adapter is None else getattr(
        adapter, "band_wavelengths_micron", ()
    )
    wavelength = dict(wavelengths).get(value)
    if wavelength is not None:
        return wavelength
    if provider == "2mass":
        read_mode_band = re.sub(r"^2MR[12]", "2M", value)
        return dict(wavelengths).get(read_mode_band)
    return None


def catalog_position_matches_components(provider: str) -> bool:
    """Whether a close catalog position can identify a system component."""
    return provider not in _SYSTEM_SCALE_POSITION_PROVIDERS

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
    "catalog_candidate_identifiers",
    "catalog_band_wavelength_micron",
    "catalog_position_matches_components",
]
