from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re

from astroquery.vizier import Vizier

from ...astroquery_config import configure_vizier_class
from ..registry import catalog_provider
from ..types import CatalogCandidate, CatalogQueryContext, MeasurementValue
from ..provenance import (
    CatalogProvenance,
    vizier_entry_url,
    vizier_readme_url,
    with_payload_provenance,
)
from ...providers import ProviderError
from .vizier import row_float, row_payload, row_text
from .review_metadata import add_review_metadata, PositionUncertainty, ReviewField


_GAIA_DR3_IDENTIFIER = re.compile(r"^Gaia\s+DR3\s+(\d+)$", re.IGNORECASE)
_PROVIDER = catalog_provider("gaia_dr3")


class GaiaDr3Adapter:
    """Retrieve native Gaia DR3 photometry for an established Gaia source."""

    # Catalog identity and source-ID-based query policy.
    name = _PROVIDER.key
    display_name = _PROVIDER.display_name
    release = _PROVIDER.catalog
    query_epoch = _PROVIDER.query_epoch
    timeout_seconds = 30.0
    query_many_workers = 4
    bibcode = _PROVIDER.bibliography
    # Native photometric bands and their flux/quality support columns.
    bands = (
        ("GAIA.G", "Gmag", "e_Gmag", "FG", "e_FG", "o_Gmag", None, None),
        (
            "GAIA.BP", "BPmag", "e_BPmag", "FBP", "e_FBP", "o_BPmag",
            "NBPcont", "NBPblend",
        ),
        (
            "GAIA.RP", "RPmag", "e_RPmag", "FRP", "e_FRP", "o_RPmag",
            "NRPcont", "NRPblend",
        ),
    )
    band_wavelengths_micron = _PROVIDER.bands
    # Provider columns exposed directly to match review.
    review_fields = (
        ReviewField(
            "single_star_probability", "single-star probability",
            ("PSS", "classprob_dsc_combmod_star"),
        ),
    )
    position_uncertainty = PositionUncertainty(
        major_columns=("e_RA_ICRS", "ra_error"),
        minor_columns=("e_DE_ICRS", "dec_error"),
        scale_to_arcsec=0.001,
        kind="coordinate_errors",
    )

    @staticmethod
    def display_source_id(source_id: str) -> str:
        value = " ".join(str(source_id).strip().split())
        matched = _GAIA_DR3_IDENTIFIER.match(value)
        return f"Gaia DR3 {matched.group(1) if matched else value}"

    @staticmethod
    def source_id_matches_identifiers(
        source_id: str,
        identifiers: tuple[str, ...],
    ) -> bool:
        return any(
            matched is not None and matched.group(1) == str(source_id).strip()
            for matched in (_GAIA_DR3_IDENTIFIER.match(value.strip()) for value in identifiers)
        )

    def create_client(self):
        configure_vizier_class(Vizier)
        return Vizier(columns=["**"], row_limit=1)

    @staticmethod
    def source_id(context: CatalogQueryContext) -> str | None:
        for identifier in context.identifiers:
            matched = _GAIA_DR3_IDENTIFIER.match(identifier.strip())
            if matched:
                return matched.group(1)
        if (
            context.astrometry.source == "gaia_dr3"
            and context.astrometry.source_id
            and str(context.astrometry.source_id).isdigit()
        ):
            return str(context.astrometry.source_id)
        return None

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
        # Single-source VizieR lookup. Identity has already selected the Gaia
        # source; this adapter does not perform another positional association.
        source_id = self.source_id(context)
        if source_id is None:
            return []
        client = self.create_client()
        client.TIMEOUT = self.timeout_seconds
        try:
            tables = client.query_constraints(catalog=self.release, Source=source_id)
        except Exception as error:
            raise ProviderError(
                f"{self.display_name} VizieR query failed: {error}", transient=True
            ) from error
        if not tables:
            return []
        candidates = [
            self._with_provenance(
                self.parse_row(row), service="VizieR", table_id=self.release,
            )
            for row in tables[0]
        ]
        return [candidate for candidate in candidates if candidate.source_id == source_id]

    def query_many(
        self, contexts: tuple[CatalogQueryContext, ...]
    ) -> dict[int, list[CatalogCandidate]]:
        result = {context.target_id: [] for context in contexts}
        selected = tuple(
            context for context in contexts if self.source_id(context) is not None
        )
        if not selected:
            return result
        # Use the same bounded VizieR source-ID lookup as the single-target path.
        # Astroquery's asynchronous Gaia TAP jobs do not honour the adapter's
        # HTTP timeout while polling and can otherwise strand a whole batch.
        max_workers = max(1, min(self.query_many_workers, len(selected)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.query, context): context
                for context in selected
            }
            for future in as_completed(futures):
                context = futures[future]
                try:
                    result[context.target_id] = future.result()
                except ProviderError:
                    raise
                except Exception as error:
                    raise ProviderError(
                        f"{self.display_name} VizieR query_many failed for "
                        f"{context.sdbid}: {error}",
                        transient=True,
                    ) from error
        return result

    @classmethod
    def _with_provenance(
        cls,
        candidate: CatalogCandidate,
        *,
        service: str,
        table_id: str,
    ) -> CatalogCandidate:
        provenance = (CatalogProvenance(
            service=service,
            catalog_id=cls.release,
            table_id=cls.release,
            identifier_column="Source",
            identifier_value=candidate.source_id,
            access_url=vizier_entry_url(
                cls.release, "Source", candidate.source_id
            ),
            readme_url=vizier_readme_url(cls.release.rsplit("/", 1)[0]),
        ),)
        payload = with_payload_provenance(candidate.payload, provenance)
        return CatalogCandidate(
            source_id=candidate.source_id,
            ra_deg=candidate.ra_deg,
            dec_deg=candidate.dec_deg,
            epoch=candidate.epoch,
            payload=payload,
            measurements=candidate.measurements,
            attributes=candidate.attributes,
            detection_key=candidate.detection_key,
            provenance=provenance,
        )

    @classmethod
    def parse_row(cls, row) -> CatalogCandidate:
        # Stable source identity and Gaia DR3 epoch-2016 position.
        source_id = row_text(row, "Source", "source_id")
        ra = row_float(row, "RA_ICRS", "RAdeg", "ra")
        dec = row_float(row, "DE_ICRS", "DEdeg", "dec")
        if not source_id or ra is None or dec is None:
            raise ProviderError(
                f"{cls.display_name} response omitted identifier or coordinates"
            )

        # Native magnitudes plus provider flux/observation/blend diagnostics.
        measurements = []
        for (
            band,
            magnitude_column,
            error_column,
            flux_column,
            flux_error_column,
            observations_column,
            contamination_column,
            blend_column,
        ) in cls.bands:
            magnitude = row_float(row, magnitude_column)
            if magnitude is None:
                continue
            flux = row_float(row, flux_column)
            flux_error = row_float(row, flux_error_column)
            observations = row_float(row, observations_column)
            contaminated = (
                row_float(row, contamination_column) if contamination_column else None
            )
            blended = row_float(row, blend_column) if blend_column else None
            note_parts = []
            if flux is not None:
                note_parts.append(f"flux:{flux:.8g} e-/s")
            if flux_error is not None:
                note_parts.append(f"e_flux:{flux_error:.8g} e-/s")
            quality_parts = []
            if observations is not None:
                quality_parts.append(f"n_obs={int(observations)}")
            if contaminated is not None:
                quality_parts.append(f"n_cont={int(contaminated)}")
            if blended is not None:
                quality_parts.append(f"n_blend={int(blended)}")
            provider_flagged = bool((contaminated or 0) > 0 or (blended or 0) > 0)
            excess = row_float(row, "E(BP/RP)", "bp_rp_excess_factor")
            measurements.append(
                MeasurementValue(
                    band=band,
                    value=magnitude,
                    error=row_float(row, error_column) or 0.0,
                    unit="mag",
                    bibcode=cls.bibcode,
                    quality=";".join(quality_parts) or None,
                    note1="; ".join(note_parts),
                    note2=f"BP/RP excess:{excess:.6g}" if excess is not None else "",
                    blend_state="blended" if provider_flagged else "clear",
                    blend_reason="provider_flagged" if provider_flagged else None,
                )
            )
        # Preserve the complete provider row and normalized review metadata.
        payload = add_review_metadata(
            row_payload(row),
            fields=cls.review_fields,
            position_uncertainty=cls.position_uncertainty,
        )
        return CatalogCandidate(
            source_id=source_id,
            ra_deg=ra,
            dec_deg=dec,
            epoch=2016.0,
            payload=payload,
            measurements=tuple(measurements),
        )

    def score_candidate(
        self,
        context: CatalogQueryContext,
        candidate: CatalogCandidate,
        separation_arcsec: float,
    ) -> float:
        return 1.0 if candidate.source_id == self.source_id(context) else 0.0

    @staticmethod
    def normalize(candidate: CatalogCandidate) -> tuple[MeasurementValue, ...]:
        return candidate.measurements
