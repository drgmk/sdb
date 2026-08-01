"""Small catalog display and association policy helpers."""

from __future__ import annotations

from collections.abc import Mapping
import re

from .registry import REMOTE_CATALOG_PROVIDERS
from ..identifiers import normalize_identifier
from .reference_definitions import SNAPSHOT_CATALOGS


_SYSTEM_SCALE_POSITION_PROVIDERS = {"iras_fsc", "iras_psc", "submm_obs"}


def _remote_adapter_class(provider: str):
    if provider not in REMOTE_CATALOG_PROVIDERS:
        return None
    if provider == "2mass":
        from .adapters.twomass import TwoMassAdapter
        return TwoMassAdapter
    if provider == "allwise":
        from .adapters.allwise import AllWiseAdapter
        return AllWiseAdapter
    if provider == "gaia_dr3":
        from .adapters.gaia import GaiaDr3Adapter
        return GaiaDr3Adapter
    if provider == "tycho2":
        from .adapters.tycho2 import Tycho2Adapter
        return Tycho2Adapter
    return None


def _catalog_identifier_key(value: str) -> str:
    normalized = normalize_identifier(value)
    matched = re.fullmatch(r"(HD|HIP|GL|GJ|LHS)\s*0*(\d+)", normalized)
    if matched is not None:
        return f"{matched.group(1)} {int(matched.group(2))}"
    return normalized


def catalog_candidate_identifiers(
    provider: str, source_id: str, payload: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    definition = SNAPSHOT_CATALOGS.get(provider)
    if definition is not None and payload is not None:
        return definition.identifiers(dict(payload))
    adapter = _remote_adapter_class(provider)
    formatter = None if adapter is None else getattr(adapter, "display_source_id", None)
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
    definition = SNAPSHOT_CATALOGS.get(provider)
    if definition is not None and payload is not None:
        candidate_keys = {
            _catalog_identifier_key(value)
            for value in catalog_candidate_identifiers(provider, source_id, payload)
            if value
        }
        identifier_keys = {_catalog_identifier_key(value) for value in identifiers if value}
        return bool(candidate_keys & identifier_keys)
    adapter = _remote_adapter_class(provider)
    if adapter is not None:
        return adapter.source_id_matches_identifiers(source_id, identifiers)
    normalized = _catalog_identifier_key(source_id)
    return normalized in {_catalog_identifier_key(value) for value in identifiers if value}


def catalog_source_display_name(
    provider: str, source_id: str, payload: Mapping[str, object] | None = None,
) -> str:
    definition = SNAPSHOT_CATALOGS.get(provider)
    if definition is not None and payload is not None:
        identifiers = catalog_candidate_identifiers(provider, source_id, payload)
        if identifiers:
            return identifiers[0]
    adapter = _remote_adapter_class(provider)
    formatter = None if adapter is None else getattr(adapter, "display_source_id", None)
    if formatter is not None:
        return str(formatter(source_id))
    return str(source_id).strip()


def catalog_band_wavelength_micron(provider: str, band: str) -> float | None:
    value = str(band).strip().upper()
    if provider == "submm_obs" and value.startswith("WAV"):
        try:
            return float(value[3:])
        except ValueError:
            return None
    definition = SNAPSHOT_CATALOGS.get(provider)
    if definition is not None:
        return dict(definition.band_wavelengths_micron).get(value)
    adapter = _remote_adapter_class(provider)
    wavelengths = () if adapter is None else getattr(adapter, "band_wavelengths_micron", ())
    wavelength = dict(wavelengths).get(value)
    if wavelength is not None:
        return wavelength
    if provider == "2mass":
        return dict(wavelengths).get(re.sub(r"^2MR[12]", "2M", value))
    return None


def catalog_position_matches_components(provider: str) -> bool:
    return provider not in _SYSTEM_SCALE_POSITION_PROVIDERS
