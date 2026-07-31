from __future__ import annotations

import math

import astropy.units as u
from astropy.coordinates import SkyCoord

from ..astrometry import propagate_to_epoch
from ..catalog_registry import catalog_provider
from ..catalog_types import CatalogCandidate, MeasurementValue
from ..catalog_provenance import (
    CatalogProvenance,
    vizier_access_url,
    vizier_readme_url,
    with_payload_provenance,
)
from ..catalog_types import CatalogQueryContext
from ..providers import Astrometry, ProviderError
from .vizier import VizierConeAdapter, row_float, row_payload, row_text, row_value
from .review_metadata import add_review_metadata, PositionUncertainty, ReviewField


_PROVIDER = catalog_provider("tycho2")


class Tycho2Adapter(VizierConeAdapter):
    # Catalog identity, table epochs, and match policy.
    name = _PROVIDER.key
    display_name = _PROVIDER.display_name
    release = _PROVIDER.catalog
    query_epoch = _PROVIDER.query_epoch
    radius_arcsec = _PROVIDER.radius_arcsec
    review_radius_arcsec = _PROVIDER.review_radius_arcsec
    bibcode = _PROVIDER.bibliography
    source_id_columns = ()
    identifier_prefixes = ("TYC ",)
    band_wavelengths_micron = _PROVIDER.bands
    # suppl_2 is retained by the upstream catalogue for completeness, but its
    # ReadMe describes the entries as probably false or heavily disturbed.
    # It is therefore intentionally not eligible for SDB matching/photometry.
    science_tables = tuple(
        (table, 2000.0 if table.endswith("/tyc2") else 1991.25)
        for table in _PROVIDER.science_tables
    )
    # Provider columns exposed directly to match review.
    review_fields = (
        ReviewField(
            "nearest_source_arcsec", "nearest catalog source", ("prox",),
            unit="arcsec", neighbourhood=True, scale=0.1,
        ),
        ReviewField("mean_position_flag", "mean-position flag", ("pflag",)),
        ReviewField("position_solution_flag", "position-solution flag", ("posflg",)),
    )
    position_uncertainty = PositionUncertainty(
        major_columns=("e_RAdeg",),
        minor_columns=("e_DEdeg",),
        scale_to_arcsec=0.001,
        kind="coordinate_errors",
    )

    @staticmethod
    def display_source_id(source_id: str) -> str:
        value = " ".join(str(source_id).strip().split())
        return value if value.upper().startswith("TYC ") else f"TYC {value}"

    @staticmethod
    def normalize_source_id(value: str) -> str:
        normalized = VizierConeAdapter.normalize_source_id(value)
        return normalized[3:] if normalized.startswith("TYC") else normalized

    def score_candidate(self, context, candidate, separation_arcsec):
        positional = math.exp(-0.5 * (separation_arcsec / 2.0) ** 2)
        expected = self.expected_source_ids(context.identifiers)
        if not expected:
            return positional
        agrees = self.normalize_source_id(candidate.source_id) in expected
        # A component-specific SIMBAD TYC identifier is strong evidence in
        # this catalog. Keep disagreeing positional rows as review candidates.
        return 1.0 if agrees else positional * 0.25

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
        # The main catalog and supplements have different native epochs and
        # search radii; query them explicitly rather than hiding that detail.
        client = self.create_client()
        client.TIMEOUT = self.timeout_seconds
        candidates = []
        expected = self.expected_source_ids(context.identifiers)
        queries = tuple(
            (
                catalog,
                epoch,
                self.radius_arcsec if catalog.endswith("/tyc2")
                else max(10.0, self.radius_arcsec),
            )
            for catalog, epoch in self.science_tables
        )
        try:
            for catalog, epoch, radius in queries:
                position = propagate_to_epoch(context.astrometry, epoch)
                coordinate = SkyCoord(
                    position.ra_deg * u.deg, position.dec_deg * u.deg
                )
                tables = client.query_region(
                    coordinate, radius=radius * u.arcsec, catalog=catalog
                )
                for table in tables:
                    table_name = (
                        table.meta.get("name") or table.meta.get("ID") or catalog
                    )
                    for row in table:
                        candidate = self.parse_row(row, table_name=table_name)
                        agreement = (
                            self.normalize_source_id(candidate.source_id) in expected
                        )
                        payload = dict(candidate.payload)
                        payload["_sdb_association"] = {
                            "method": "position+identifier" if expected else "position",
                            "expected_source_ids": sorted(expected),
                            "identifier_agreement": agreement,
                            "identifier_policy": "simbad_tyc_component",
                        }
                        candidates.append(CatalogCandidate(
                            source_id=candidate.source_id,
                            ra_deg=candidate.ra_deg,
                            dec_deg=candidate.dec_deg,
                            epoch=candidate.epoch,
                            payload=payload,
                            measurements=candidate.measurements,
                            attributes=candidate.attributes,
                            detection_key=candidate.detection_key,
                            provenance=candidate.provenance,
                        ))
        except Exception as error:
            raise ProviderError(
                f"{self.display_name} VizieR query failed: {error}", transient=True
            ) from error
        return self._merge_duplicate_sources(candidates)

    def query_many(
        self, contexts: tuple[CatalogQueryContext, ...]
    ) -> dict[int, list[CatalogCandidate]]:
        """Batch the main and supplement cones while preserving table epochs."""
        result: dict[int, list[CatalogCandidate]] = {
            context.target_id: [] for context in contexts
        }
        queries = tuple(
            (
                catalog,
                epoch,
                self.radius_arcsec if catalog.endswith("/tyc2")
                else max(10.0, self.radius_arcsec),
            )
            for catalog, epoch in self.science_tables
        )
        client = self.create_client()
        client.TIMEOUT = self.timeout_seconds
        try:
            for catalog, epoch, radius in queries:
                for offset in range(0, len(contexts), self.multicone_chunk_size):
                    chunk = contexts[offset:offset + self.multicone_chunk_size]
                    if not chunk:
                        continue
                    positions = [
                        propagate_to_epoch(context.astrometry, epoch)
                        for context in chunk
                    ]
                    coordinate = SkyCoord(
                        [position.ra_deg for position in positions] * u.deg,
                        [position.dec_deg for position in positions] * u.deg,
                    )
                    tables = client.query_region(
                        coordinate, radius=radius * u.arcsec, catalog=catalog
                    )
                    for table in tables:
                        table_name = (
                            table.meta.get("name") or table.meta.get("ID") or catalog
                        )
                        for row in table:
                            query_index = row_value(row, "_q")
                            context = chunk[int(query_index) - 1]
                            candidate = self.parse_row(row, table_name=table_name)
                            expected = self.expected_source_ids(context.identifiers)
                            agreement = (
                                self.normalize_source_id(candidate.source_id) in expected
                            )
                            payload = dict(candidate.payload)
                            payload["_sdb_association"] = {
                                "method": "position+identifier" if expected else "position",
                                "query_service": "VizieR multi-cone",
                                "query_catalog": catalog,
                                "expected_source_ids": sorted(expected),
                                "identifier_agreement": agreement,
                                "identifier_policy": "simbad_tyc_component",
                            }
                            result[context.target_id].append(CatalogCandidate(
                                source_id=candidate.source_id,
                                ra_deg=candidate.ra_deg,
                                dec_deg=candidate.dec_deg,
                                epoch=candidate.epoch,
                                payload=payload,
                                measurements=candidate.measurements,
                                attributes=candidate.attributes,
                                detection_key=candidate.detection_key,
                                provenance=candidate.provenance,
                            ))
        except Exception as error:
            raise ProviderError(
                f"{self.display_name} VizieR multi-cone query failed: {error}",
                transient=True,
            ) from error
        return {
            target_id: self._merge_duplicate_sources(candidates)
            for target_id, candidates in result.items()
        }

    @staticmethod
    def _merge_duplicate_sources(
        candidates: list[CatalogCandidate],
    ) -> list[CatalogCandidate]:
        """Prefer the first science table but retain every table provenance."""

        result: dict[str, CatalogCandidate] = {}
        for candidate in candidates:
            previous = result.get(candidate.source_id)
            if previous is None:
                result[candidate.source_id] = candidate
                continue
            provenance = tuple(dict.fromkeys(
                (*previous.provenance, *candidate.provenance)
            ))
            payload = dict(previous.payload)
            payload["_sdb_duplicate_tables"] = [
                item.table_id for item in provenance if item.table_id
            ]
            payload = with_payload_provenance(payload, provenance)
            result[candidate.source_id] = CatalogCandidate(
                source_id=previous.source_id,
                ra_deg=previous.ra_deg,
                dec_deg=previous.dec_deg,
                epoch=previous.epoch,
                payload=payload,
                measurements=previous.measurements,
                attributes=previous.attributes,
                detection_key=previous.detection_key,
                provenance=provenance,
            )
        return list(result.values())

    @classmethod
    def parse_row(cls, row, *, table_name: str | None = None) -> CatalogCandidate:
        # Stable Tycho identity and table-native coordinates.
        table_name = table_name or row_text(row, "_table") or "I/259/tyc2"
        parts = [row_text(row, column) for column in ("TYC1", "TYC2", "TYC3")]
        ra = row_float(row, "RAmdeg", "RA_ICRS", "RAJ2000", "RA(ICRS)")
        dec = row_float(row, "DEmdeg", "DE_ICRS", "DEJ2000", "DE(ICRS)")
        if not all(parts) or ra is None or dec is None:
            raise ProviderError(
                f"{cls.display_name} response omitted identifier or coordinates"
            )
        source_id = "TYC " + "-".join(parts)
        supplement = table_name.endswith(("/suppl_1", "/suppl_2"))
        if supplement:
            propagated = propagate_to_epoch(Astrometry(
                ra,
                dec,
                1991.25,
                pm_ra_cosdec_masyr=row_float(row, "pmRA"),
                pm_dec_masyr=row_float(row, "pmDE"),
            ), cls.query_epoch)
            ra, dec = propagated.ra_deg, propagated.dec_deg
        # Provider quality and component/photocentre flags. pflag/posflg belong
        # to the main table; mflag belongs to the two
        # supplements. Keep the table-specific schemas explicit here.
        mean_position_flag = "" if supplement else (row_text(row, "pflag") or "")
        position_flag = "" if supplement else (row_text(row, "posflg") or "")
        photocentre = "P" in {mean_position_flag, position_flag}
        proximity = row_text(row, "prox") or ""
        magnitude_flag = (row_text(row, "mflag") or "") if supplement else ""
        # Native BT/VT photometry; no color or passband conversion is applied.
        measurements = []
        for band, value_column, error_column in (
            ("BT", "BTmag", "e_BTmag"),
            ("VT", "VTmag", "e_VTmag"),
        ):
            value = row_float(row, value_column)
            if band == "VT" and magnitude_flag == "H":
                value = None
            if value is None:
                continue
            measurements.append(MeasurementValue(
                band=band,
                value=value,
                error=row_float(row, error_column) or 0.0,
                unit="mag",
                bibcode=cls.bibcode,
                quality=magnitude_flag or position_flag or mean_position_flag or None,
                note1=f"pflag:{mean_position_flag};posflg:{position_flag}",
                note2=f"prox:{proximity};table:{table_name.rsplit('/', 1)[-1]}",
                ownership_scope="system" if photocentre else "component",
                blend_state="blended" if photocentre else "clear",
                blend_reason="provider_flagged" if photocentre else None,
            ))
        # Preserve the complete provider row and normalized review metadata.
        payload = add_review_metadata(
            {**row_payload(row), "_table": table_name},
            fields=cls.review_fields,
            position_uncertainty=cls.position_uncertainty,
        )
        provenance = (CatalogProvenance(
            service="VizieR",
            catalog_id=cls.release,
            table_id=table_name,
            access_url=vizier_access_url(table_name),
            readme_url=vizier_readme_url(cls.release),
        ),)
        payload = with_payload_provenance(payload, provenance)
        return CatalogCandidate(
            source_id=source_id,
            ra_deg=ra,
            dec_deg=dec,
            epoch=cls.query_epoch,
            payload=payload,
            measurements=tuple(measurements),
            provenance=provenance,
        )
