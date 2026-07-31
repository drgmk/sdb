from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol


@dataclass(frozen=True)
class Astrometry:
    ra_deg: float
    dec_deg: float
    epoch: float = 2000.0
    pm_ra_cosdec_masyr: float | None = None
    pm_dec_masyr: float | None = None
    parallax_mas: float | None = None
    radial_velocity_kms: float | None = None
    source: str = "input"
    source_id: str | None = None
    position_bibcode: str | None = None
    proper_motion_bibcode: str | None = None
    parallax_bibcode: str | None = None
    radial_velocity_bibcode: str | None = None

    @property
    def proper_motion_available(self) -> bool:
        return self.pm_ra_cosdec_masyr is not None and self.pm_dec_masyr is not None

    def with_source(self, source: str, source_id: str | None = None) -> "Astrometry":
        return replace(self, source=source, source_id=source_id)


@dataclass(frozen=True)
class NameResolution:
    main_id: str
    astrometry: Astrometry
    identifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    source_id: str
    astrometry: Astrometry
    identifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimbadNeighbour:
    oid: int
    main_id: str
    astrometry: Astrometry
    separation_arcsec: float
    primary_object_type: str | None = None
    object_type_label: str | None = None
    object_type_description: str | None = None
    object_types: tuple[str, ...] = ()
    spectral_type: str | None = None


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class SimbadProvider(Protocol):
    name: str

    def resolve_name(self, name: str) -> NameResolution | None: ...


class SimbadDiscoveryProvider(Protocol):
    name: str

    def search_region(
        self,
        astrometry: Astrometry,
        *,
        radius_arcsec: float,
        limit: int = 100,
    ) -> list[SimbadNeighbour]: ...


class GaiaProvider(Protocol):
    name: str

    def search(self, astrometry: Astrometry) -> list[Candidate]: ...


class NullSimbad:
    name = "simbad"

    def resolve_name(self, name: str) -> NameResolution | None:
        return None


class NullGaia:
    name = "gaia_dr3"

    def search(self, astrometry: Astrometry) -> list[Candidate]:
        return []
