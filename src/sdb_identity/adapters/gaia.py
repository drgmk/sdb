from __future__ import annotations

import re

from astropy.table import Table
from astroquery.vizier import Vizier

from ..astroquery_config import configure_vizier_class
from ..catalogs import CatalogCandidate, CatalogQueryContext, MeasurementValue
from ..catalog_provenance import (
    CatalogProvenance,
    vizier_entry_url,
    vizier_readme_url,
    with_payload_provenance,
)
from ..providers import ProviderError
from .vizier import row_float, row_payload, row_text
from .review_metadata import add_review_metadata, PositionUncertainty, ReviewField


_GAIA_DR3_IDENTIFIER = re.compile(r"^Gaia\s+DR3\s+(\d+)$", re.IGNORECASE)


class GaiaDr3Adapter:
    """Retrieve native Gaia DR3 photometry for an established Gaia source."""

    # Catalog identity and source-ID-based query policy.
    name = "gaia_dr3"
    display_name = "Gaia DR3"
    release = "I/355/gaiadr3"
    query_epoch = 2016.0
    timeout_seconds = 30.0
    bibcode = "2023A&A...674A...1G"
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
    band_wavelengths_micron = (
        ("GAIA.BP", 0.5129),
        ("GAIA.G", 0.6425),
        ("GAIA.RP", 0.7799),
    )
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

    def create_bulk_client(self):
        from astroquery.gaia import Gaia

        return Gaia

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
        rows = [
            (context.target_id, int(source_id))
            for context in contexts
            if (source_id := self.source_id(context)) is not None
        ]
        if not rows:
            return result
        # Bulk TAP lookup of exactly the established Gaia source IDs.
        upload = Table(rows=rows, names=("input_target_id", "source_id"))
        query = """
            SELECT t.input_target_id, g.source_id AS Source,
                   g.ra AS RA_ICRS, g.dec AS DE_ICRS,
                   g.ra_error AS e_RA_ICRS, g.dec_error AS e_DE_ICRS,
                   g.phot_g_mean_mag AS Gmag,
                   1.0857362047581294*g.phot_g_mean_flux_error/g.phot_g_mean_flux AS e_Gmag,
                   g.phot_g_mean_flux AS FG, g.phot_g_mean_flux_error AS e_FG,
                   g.phot_g_n_obs AS o_Gmag,
                   g.phot_bp_mean_mag AS BPmag,
                   1.0857362047581294*g.phot_bp_mean_flux_error/g.phot_bp_mean_flux AS e_BPmag,
                   g.phot_bp_mean_flux AS FBP, g.phot_bp_mean_flux_error AS e_FBP,
                   g.phot_bp_n_obs AS o_BPmag,
                   g.phot_bp_n_contaminated_transits AS NBPcont,
                   g.phot_bp_n_blended_transits AS NBPblend,
                   g.phot_rp_mean_mag AS RPmag,
                   1.0857362047581294*g.phot_rp_mean_flux_error/g.phot_rp_mean_flux AS e_RPmag,
                   g.phot_rp_mean_flux AS FRP, g.phot_rp_mean_flux_error AS e_FRP,
                   g.phot_rp_n_obs AS o_RPmag,
                   g.phot_rp_n_contaminated_transits AS NRPcont,
                   g.phot_rp_n_blended_transits AS NRPblend,
                   g.phot_bp_rp_excess_factor AS bp_rp_excess_factor,
                   g.classprob_dsc_combmod_star AS PSS
            FROM tap_upload.targets AS t
            JOIN gaiadr3.gaia_source AS g ON g.source_id=t.source_id
        """
        client = self.create_bulk_client()
        try:
            job = client.launch_job_async(
                query,
                upload_resource=upload,
                upload_table_name="targets",
                dump_to_file=False,
            )
            returned = job.get_results()
        except Exception as error:
            raise ProviderError(
                f"{self.display_name} bulk Gaia TAP query failed: {error}",
                transient=True,
            ) from error
        for row in returned:
            target_id = int(row["input_target_id"])
            result.setdefault(target_id, []).append(self._with_provenance(
                self.parse_row(row),
                service="Gaia TAP",
                table_id="gaiadr3.gaia_source",
            ))
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
