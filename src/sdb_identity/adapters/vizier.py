from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import astropy.units as u
from astropy.coordinates import SkyCoord
from astroquery.vizier import Vizier

from ..astroquery_config import configure_vizier_class
from ..astrometry import angular_separation_arcsec
from ..catalogs import CatalogCandidate, CatalogQueryContext, MeasurementValue
from ..providers import ProviderError


def row_value(row: Any, *names: str):
    columns = {str(name).lower(): name for name in getattr(row, "colnames", ())}
    if isinstance(row, dict):
        columns.update({str(name).lower(): name for name in row})
    for name in names:
        key = columns.get(name.lower())
        if key is None:
            continue
        value = row[key]
        mask = getattr(value, "mask", False)
        try:
            masked = bool(mask) if not hasattr(mask, "any") else bool(mask.any())
        except ValueError:
            masked = True
        if masked or value is None:
            # A provider may expose several aliases while masking its native
            # field and populating a derived coordinate or identifier column.
            continue
        return value.item() if hasattr(value, "item") and getattr(value, "ndim", 0) == 0 else value
    return None


def row_text(row: Any, *names: str) -> str | None:
    value = row_value(row, *names)
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return str(value).strip()


def row_float(row: Any, *names: str) -> float | None:
    value = row_value(row, *names)
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


# Re-exported from the shared serialization module (kept here for the many
# call sites that import them from this adapter).
from ..serialization import json_value as _json_value, row_payload  # noqa: E402,F401


@dataclass(frozen=True)
class BandDefinition:
    band: str
    value_column: str
    error_column: str
    systematic_error: float
    unit: str = "mag"

    def value(self, row: Any) -> float | None:
        return row_float(row, self.value_column)

    def error(self, row: Any) -> float:
        return row_float(row, self.error_column) or 0.0


class VizierConeAdapter(ABC):
    """Shared query/matching mechanics; subclasses own catalog semantics."""

    # Query contract and generic cone-search defaults.
    name: str
    display_name: str
    release: str
    query_epoch: float
    radius_arcsec = 2.0
    review_radius_arcsec: float | None = None
    timeout_seconds = 30.0
    row_limit = 10
    columns = ("**", "+_r")
    source_id_columns: tuple[str, ...]
    ra_columns = ("RAJ2000", "RA_ICRS")
    dec_columns = ("DEJ2000", "DE_ICRS")
    identifier_prefixes: tuple[str, ...] = ()
    query_many_workers = 4
    multicone_chunk_size = 250

    def create_client(self):
        configure_vizier_class(Vizier)
        return Vizier(columns=list(self.columns), row_limit=self.row_limit)

    def acceptance_radius(self, context: CatalogQueryContext) -> float:
        return self.radius_arcsec

    def query_radius(self, context: CatalogQueryContext) -> float:
        acceptance_radius = self.acceptance_radius(context)
        review_radius = self.review_radius_arcsec
        if review_radius is None:
            return acceptance_radius
        return max(acceptance_radius, review_radius)

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
        # Provider transport and response attribution.
        coordinate = SkyCoord(
            context.astrometry.ra_deg * u.deg,
            context.astrometry.dec_deg * u.deg,
            frame="icrs",
        )
        client = self.create_client()
        client.TIMEOUT = self.timeout_seconds
        radius_arcsec = self.query_radius(context)
        try:
            tables = client.query_region(
                coordinate,
                radius=radius_arcsec * u.arcsec,
                catalog=self.release,
            )
        except Exception as error:
            raise ProviderError(
                f"{self.display_name} VizieR query failed: {error}",
                transient=True,
            ) from error
        if not tables:
            return []
        acceptance_radius_arcsec = self.acceptance_radius(context)
        expected = self.expected_source_ids(context.identifiers)
        return [
            self.annotate_candidate(
                self.parse_row(row),
                context=context,
                query_radius_arcsec=radius_arcsec,
                acceptance_radius_arcsec=acceptance_radius_arcsec,
                expected=expected,
            )
            for row in tables[0]
        ]

    def query_many(
        self, contexts: tuple[CatalogQueryContext, ...]
    ) -> dict[int, list[CatalogCandidate]]:
        result = {context.target_id: [] for context in contexts}
        if not contexts:
            return result
        max_workers = max(1, min(self.query_many_workers, len(contexts)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.query, context): context
                for context in contexts
            }
            for future in as_completed(futures):
                context = futures[future]
                try:
                    result[context.target_id] = future.result()
                except ProviderError:
                    raise
                except Exception as error:
                    raise ProviderError(
                        f"{self.display_name} VizieR query_many failed for {context.sdbid}: {error}",
                        transient=True,
                    ) from error
        return result

    def query_many_vizier(
        self, contexts: tuple[CatalogQueryContext, ...]
    ) -> dict[int, list[CatalogCandidate]]:
        """Query several cones in one VizieR request and distribute by ``_q``."""
        result = {context.target_id: [] for context in contexts}
        for offset in range(0, len(contexts), self.multicone_chunk_size):
            chunk = contexts[offset:offset + self.multicone_chunk_size]
            if not chunk:
                continue
            coordinate = SkyCoord(
                [context.astrometry.ra_deg for context in chunk] * u.deg,
                [context.astrometry.dec_deg for context in chunk] * u.deg,
                frame="icrs",
            )
            radius_arcsec = max(self.query_radius(context) for context in chunk)
            client = self.create_client()
            client.TIMEOUT = self.timeout_seconds
            try:
                tables = client.query_region(
                    coordinate,
                    radius=radius_arcsec * u.arcsec,
                    catalog=self.release,
                )
            except Exception as error:
                raise ProviderError(
                    f"{self.display_name} VizieR multi-cone query failed: {error}",
                    transient=True,
                ) from error
            if not tables:
                continue
            for table in tables:
                for row in table:
                    query_index = row_value(row, "_q")
                    try:
                        context = chunk[int(query_index) - 1]
                    except (TypeError, ValueError, IndexError) as error:
                        raise ProviderError(
                            f"{self.display_name} VizieR multi-cone response omitted a valid _q index",
                            transient=True,
                        ) from error
                    result[context.target_id].append(self.annotate_candidate(
                        self.parse_row(row),
                        context=context,
                        query_radius_arcsec=radius_arcsec,
                        acceptance_radius_arcsec=self.acceptance_radius(context),
                        expected=self.expected_source_ids(context.identifiers),
                        query_service="VizieR multi-cone",
                    ))
        return result

    @staticmethod
    def normalize_source_id(value: str) -> str:
        return "".join(str(value).upper().split())

    @classmethod
    def expected_source_ids(cls, identifiers: tuple[str, ...]) -> set[str]:
        # Catalog-specific identifier prefixes are declared by each adapter;
        # this base class only applies their normalization consistently.
        expected = set()
        for identifier in identifiers:
            folded = identifier.casefold()
            for prefix in cls.identifier_prefixes:
                if folded.startswith(prefix.casefold()):
                    expected.add(cls.normalize_source_id(identifier[len(prefix):]))
                    break
        return expected

    @classmethod
    def source_id_matches_identifiers(
        cls,
        source_id: str,
        identifiers: tuple[str, ...],
    ) -> bool:
        return cls.normalize_source_id(source_id) in cls.expected_source_ids(identifiers)

    def annotate_candidate(
        self,
        candidate: CatalogCandidate,
        *,
        context: CatalogQueryContext,
        query_radius_arcsec: float,
        acceptance_radius_arcsec: float | None = None,
        expected: set[str] | None = None,
        query_service: str = "VizieR",
        query_catalog: str | None = None,
    ) -> CatalogCandidate:
        if acceptance_radius_arcsec is None:
            acceptance_radius_arcsec = self.acceptance_radius(context)
        if expected is None:
            expected = self.expected_source_ids(context.identifiers)
        agreement = self.normalize_source_id(candidate.source_id) in expected
        separation_arcsec = self.candidate_separation(context, candidate)
        review_only = separation_arcsec > acceptance_radius_arcsec
        # Association metadata is SDB review state, kept separate from the
        # provider-native columns preserved in the rest of the payload.
        payload = dict(candidate.payload)
        association = dict(payload.get("_sdb_association") or {})
        association.update({
            "method": "position+identifier" if expected else "position",
            "query_service": query_service,
            "query_catalog": self.release if query_catalog is None else query_catalog,
            "query_radius_arcsec": query_radius_arcsec,
            "acceptance_radius_arcsec": acceptance_radius_arcsec,
            "candidate_separation_arcsec": separation_arcsec,
            "review_only": review_only,
            "expected_source_ids": sorted(expected),
            "identifier_agreement": agreement,
        })
        payload["_sdb_association"] = association
        return CatalogCandidate(
            source_id=candidate.source_id,
            ra_deg=candidate.ra_deg,
            dec_deg=candidate.dec_deg,
            epoch=candidate.epoch,
            payload=payload,
            measurements=candidate.measurements,
            attributes=candidate.attributes,
        )

    def candidate_separation(
        self,
        context: CatalogQueryContext,
        candidate: CatalogCandidate,
    ) -> float:
        return angular_separation_arcsec(
            context.astrometry,
            candidate.astrometry,
            epoch=self.query_epoch,
        )

    def score_candidate(
        self,
        context: CatalogQueryContext,
        candidate: CatalogCandidate,
        separation_arcsec: float,
    ) -> float:
        association = candidate.payload.get("_sdb_association")
        if isinstance(association, dict) and association.get("review_only"):
            return 0.0
        positional = math.exp(-0.5 * (separation_arcsec / 2.0) ** 2)
        expected = self.expected_source_ids(context.identifiers)
        agrees = self.normalize_source_id(candidate.source_id) in expected
        return min(positional + (0.5 if agrees else 0.0), 1.0)

    @classmethod
    def candidate_identity(cls, row: Any) -> tuple[str, float, float]:
        source_id = row_text(row, *cls.source_id_columns)
        ra = row_float(row, *cls.ra_columns)
        dec = row_float(row, *cls.dec_columns)
        if not source_id or ra is None or dec is None:
            raise ProviderError(
                f"{cls.display_name} response omitted identifier or coordinates"
            )
        return source_id, ra, dec

    @abstractmethod
    def parse_row(self, row: Any) -> CatalogCandidate:
        raise NotImplementedError

    def normalize(self, candidate: CatalogCandidate) -> tuple[MeasurementValue, ...]:
        return candidate.measurements
