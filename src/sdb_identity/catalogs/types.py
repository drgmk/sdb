"""Transport-neutral values shared by catalog adapters and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from .provenance import CatalogProvenance
from ..providers import Astrometry
from ..vocabulary import ProviderRunStatus


@dataclass(frozen=True)
class MeasurementValue:
    band: str
    value: float
    error: float = 0.0
    systematic_error: float = 0.0
    unit: str = "mag"
    bibcode: str = ""
    quality: str | None = None
    upper_limit: bool = False
    excluded: bool = False
    exclusion_reason: str | None = None
    note1: str = ""
    note2: str = ""
    private: bool = False
    resolution_major_arcsec: float | None = None
    resolution_minor_arcsec: float | None = None
    resolution_kind: str | None = None
    resolution_reference: str | None = None
    ownership_scope: str = "component"
    blend_state: str = "clear"
    blend_reason: str | None = None
    measurement_key: str | None = None


@dataclass(frozen=True)
class CatalogAttributeValue:
    key: str
    value_text: str | None = None
    value_float: float | None = None
    uncertainty: float | None = None
    unit: str | None = None
    quality: str | None = None
    reference: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class CatalogCandidate:
    source_id: str
    ra_deg: float
    dec_deg: float
    epoch: float
    payload: Mapping[str, object]
    measurements: tuple[MeasurementValue, ...] = field(default_factory=tuple)
    attributes: tuple[CatalogAttributeValue, ...] = field(default_factory=tuple)
    detection_key: str | None = None
    provenance: tuple[CatalogProvenance, ...] = field(default_factory=tuple)

    @property
    def astrometry(self) -> Astrometry:
        return Astrometry(
            self.ra_deg, self.dec_deg, self.epoch,
            source="catalog", source_id=self.source_id,
        )


@dataclass(frozen=True)
class CatalogQueryContext:
    target_id: int
    sdbid: str
    astrometry: Astrometry
    identifiers: tuple[str, ...] = ()


class CatalogAdapter(Protocol):
    name: str
    release: str
    query_epoch: float

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]: ...
    def normalize(self, candidate: CatalogCandidate) -> tuple[MeasurementValue, ...]: ...


class BulkCatalogAdapter(CatalogAdapter, Protocol):
    def query_many(
        self, contexts: tuple[CatalogQueryContext, ...]
    ) -> Mapping[int, list[CatalogCandidate]]: ...


@dataclass(frozen=True)
class CatalogRefreshResult:
    run_id: int
    target_id: int
    provider: str
    status: ProviderRunStatus
    candidate_count: int
    measurement_count: int
    selected_source_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class DetectionNormalizationItem:
    detection_id: int
    provider: str
    source_id: str
    status: str
    measurement_count: int
    error: str | None = None


@dataclass(frozen=True)
class DetectionNormalizationSummary:
    detection_count: int
    completed: int
    no_measurements: int
    failed: int
    measurement_count: int
    items: tuple[DetectionNormalizationItem, ...]
