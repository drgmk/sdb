"""Authoritative registry for photometric catalog providers.

The registry describes operator-visible provider policy and owns adapter
construction.  Provider modules retain the code that queries and normalizes
their particular upstream catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .reference_definitions import SNAPSHOT_CATALOGS

if TYPE_CHECKING:
    from .types import CatalogAdapter
    from ..reference.store import ReferenceStore


@dataclass(frozen=True)
class CatalogProviderDefinition:
    key: str
    display_name: str
    catalog: str
    acquisition_mode: str
    science_tables: tuple[str, ...]
    query_epoch: float | None
    radius_arcsec: float | None
    review_radius_arcsec: float | None
    identifier_policy: str
    component_policy: str
    bands: tuple[tuple[str, float], ...]
    bibliography: str
    caveats: tuple[str, ...] = ()

    @property
    def is_snapshot(self) -> bool:
        return self.acquisition_mode == "reference_snapshot"


def _remote(
    key: str,
    display_name: str,
    catalog: str,
    *,
    acquisition_mode: str = "remote_cone",
    science_tables: tuple[str, ...] = (),
    query_epoch: float | None,
    radius_arcsec: float | None,
    review_radius_arcsec: float | None = None,
    identifier_policy: str,
    component_policy: str = "A close position may identify a component.",
    bands: tuple[tuple[str, float], ...],
    bibliography: str,
    caveats: tuple[str, ...] = (),
) -> CatalogProviderDefinition:
    return CatalogProviderDefinition(
        key=key,
        display_name=display_name,
        catalog=catalog,
        acquisition_mode=acquisition_mode,
        science_tables=science_tables or (catalog,),
        query_epoch=query_epoch,
        radius_arcsec=radius_arcsec,
        review_radius_arcsec=review_radius_arcsec,
        identifier_policy=identifier_policy,
        component_policy=component_policy,
        bands=bands,
        bibliography=bibliography,
        caveats=caveats,
    )


_REMOTE_PROVIDERS = (
    _remote(
        "2mass", "2MASS", "II/246/out",
        query_epoch=1999.33, radius_arcsec=2.0, review_radius_arcsec=15.0,
        identifier_policy="2MASS J source identifier; corroborate with SIMBAD aliases.",
        bands=(("2MJ", 1.2376), ("2MH", 1.6476), ("2MKS", 2.1621)),
        bibliography="2003tmc..book.....C",
        caveats=("The default VizieR backend can be changed to IRSA for comparison.",),
    ),
    _remote(
        "allwise", "AllWISE", "II/328/allwise",
        query_epoch=2010.5, radius_arcsec=2.0, review_radius_arcsec=15.0,
        identifier_policy="AllWISE source identifier; SIMBAD commonly uses WISEA J aliases.",
        bands=(
            ("WISE3P4", 3.3792), ("WISE4P6", 4.6293),
            ("WISE12", 12.3321), ("WISE22", 22.2533),
        ),
        bibliography="2010AJ....140.1868W",
        caveats=("Band-dependent resolution can require a blend decision.",),
    ),
    _remote(
        "gaia_dr3", "Gaia DR3", "I/355/gaiadr3",
        acquisition_mode="remote_identifier",
        query_epoch=2016.0, radius_arcsec=None,
        identifier_policy="Exact Gaia DR3 source identifier selected during identity resolution.",
        bands=(("GAIA.BP", 0.5129), ("GAIA.G", 0.6425), ("GAIA.RP", 0.7799)),
        bibliography="2023A&A...674A...1G",
    ),
    _remote(
        "tycho2", "Tycho-2", "I/259",
        science_tables=("I/259/tyc2", "I/259/suppl_1"),
        query_epoch=2000.0, radius_arcsec=2.0, review_radius_arcsec=15.0,
        identifier_policy="TYC identifier constructed as TYC1-TYC2-TYC3.",
        bands=(("BT", 0.4203), ("VT", 0.5317)),
        bibliography="2000A&A...355L..27H",
        caveats=(
            "I/259/suppl_2 is retained for completeness but excluded from matching and photometry.",
        ),
    ),
)


_SNAPSHOT_NAMES = {
    "gaspar13": "Gaspar et al. 2013",
    "v70a": "Catalogue of Nearby Stars (V/70A)",
    "iras_psc": "IRAS Point Source Catalog",
    "iras_fsc": "IRAS Faint Source Catalog",
    "hip2": "Hipparcos New Reduction",
    "tdsc": "Tycho Double Star Catalogue",
    "ubvmeans": "UBV Mean Photometry",
    "paunzen15": "Paunzen et al. 2015",
    "koen10": "Koen et al. 2010",
}

_SNAPSHOT_COMPONENT_POLICY = {
    "v70a": "Comp refines row identifiers to the named component.",
    "tdsc": "m_TDSC identifies the WDS component where available.",
    "ubvmeans": "m_LID distinguishes component and combined-system photometry.",
    "iras_psc": "System-scale position does not identify a component.",
    "iras_fsc": "System-scale position does not identify a component.",
}

_SNAPSHOT_CAVEATS = {
    "gaspar13": ("Reference codes link science rows to the retained refs table.",),
    "tdsc": ("Science rows may come from either I/276/catalog or I/276/supplem.",),
}


def _snapshot_definition(key: str) -> CatalogProviderDefinition:
    source = SNAPSHOT_CATALOGS[key]
    identifier_columns = [
        f"{prefix + ' ' if prefix else ''}{column}"
        for column, prefix in source.identifier_columns
    ]
    identifier_columns.extend(
        f"{prefix} {'-'.join(columns)}"
        for prefix, columns, _separator in source.composite_identifier_columns
    )
    identifier_policy = (
        ", ".join(identifier_columns)
        if identifier_columns
        else f"Catalog column {source.primary_identifier}."
    )
    if identifier_columns:
        identifier_policy += "."
    return CatalogProviderDefinition(
        key=key,
        display_name=_SNAPSHOT_NAMES[key],
        catalog=source.catalog,
        acquisition_mode="reference_snapshot",
        science_tables=source.tables_for_matching,
        query_epoch=source.query_epoch,
        radius_arcsec=source.radius_arcsec,
        review_radius_arcsec=None,
        identifier_policy=identifier_policy,
        component_policy=_SNAPSHOT_COMPONENT_POLICY.get(
            key, "A close position may identify a component."
        ),
        bands=source.band_wavelengths_micron,
        bibliography=source.bibliography,
        caveats=_SNAPSHOT_CAVEATS.get(key, ()),
    )


CATALOG_PROVIDERS = {
    definition.key: definition
    for definition in (
        *_REMOTE_PROVIDERS,
        *(_snapshot_definition(key) for key in SNAPSHOT_CATALOGS),
    )
}

REMOTE_CATALOG_PROVIDERS = tuple(
    key for key, definition in CATALOG_PROVIDERS.items()
    if not definition.is_snapshot
)
SNAPSHOT_CATALOG_PROVIDERS = tuple(
    key for key, definition in CATALOG_PROVIDERS.items()
    if definition.is_snapshot
)


def catalog_provider(key: str) -> CatalogProviderDefinition:
    try:
        return CATALOG_PROVIDERS[key]
    except KeyError as error:
        raise ValueError(f"unknown catalog provider: {key}") from error


def build_catalog_adapter(
    key: str, *, reference_store: ReferenceStore | None = None,
) -> CatalogAdapter:
    """Construct one adapter without eagerly importing every implementation."""
    definition = catalog_provider(key)
    if definition.is_snapshot:
        if reference_store is None:
            raise ValueError(f"{key} requires a reference store")
        from .adapters.reference import snapshot_adapter

        return snapshot_adapter(key, reference_store)
    factories: dict[str, Callable[[], CatalogAdapter]] = {
        "2mass": _two_mass_adapter,
        "allwise": _allwise_adapter,
        "gaia_dr3": _gaia_adapter,
        "tycho2": _tycho_adapter,
    }
    return factories[key]()


def build_catalog_adapters(
    keys: tuple[str, ...] | list[str], *, reference_store: ReferenceStore | None = None,
) -> dict[str, CatalogAdapter]:
    return {
        key: build_catalog_adapter(key, reference_store=reference_store)
        for key in keys
    }


def _two_mass_adapter() -> CatalogAdapter:
    from .adapters.twomass import TwoMassAdapter
    return TwoMassAdapter()


def _allwise_adapter() -> CatalogAdapter:
    from .adapters.allwise import AllWiseAdapter
    return AllWiseAdapter()


def _gaia_adapter() -> CatalogAdapter:
    from .adapters.gaia import GaiaDr3Adapter
    return GaiaDr3Adapter()


def _tycho_adapter() -> CatalogAdapter:
    from .adapters.tycho2 import Tycho2Adapter
    return Tycho2Adapter()
