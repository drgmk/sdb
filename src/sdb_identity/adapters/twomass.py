from __future__ import annotations

import os

import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time
from astroquery.ipac.irsa import Irsa

from ..astrometry import angular_separation_arcsec
from ..catalogs import CatalogCandidate, CatalogQueryContext, MeasurementValue
from ..providers import ProviderError
from .vizier import (
    BandDefinition,
    VizierConeAdapter,
    row_float,
    row_payload,
    row_text,
)
from .review_metadata import add_review_metadata, PositionUncertainty, ReviewField


TWOMASS_BACKEND_ENV = "SDB_2MASS_BACKEND"
TWOMASS_BACKENDS = {"vizier", "irsa"}


class TwoMassAdapter(VizierConeAdapter):
    # Catalog identity, observing epoch, and match policy.
    name = "2mass"
    display_name = "2MASS"
    release = "II/246/out"
    irsa_catalog = "fp_psc"
    # The survey ran from roughly June 1997 through March 2001. Query at the
    # midpoint with a motion-expanded cone, then score at each row's Date.
    query_epoch = 1999.33
    survey_half_span_years = 1.92
    radius_arcsec = 2.0
    review_radius_arcsec = 15.0
    bibcode = "2003tmc..book.....C"
    source_id_columns = ("_2MASS", "2MASS")
    # SIMBAD identifiers are authoritative corroborating evidence after the
    # catalog position has independently fallen inside the propagated cone.
    identifier_prefixes = ("2MASS J", "2MASS ")
    bands = (
        (0, "J", BandDefinition("2MJ", "Jmag", "e_Jmag", 0.01)),
        (1, "H", BandDefinition("2MH", "Hmag", "e_Hmag", 0.01)),
        (2, "KS", BandDefinition("2MKS", "Kmag", "e_Kmag", 0.01)),
    )
    # Provider columns exposed directly to match review.
    review_fields = (
        ReviewField(
            "nearest_source_arcsec", "nearest catalog source", ("prox",),
            unit="arcsec", neighbourhood=True,
        ),
        ReviewField("photometry_quality", "photometry quality", ("Qflg", "ph_qual")),
        ReviewField("contamination_flags", "contamination flags", ("Cflg", "cc_flg")),
        ReviewField("extended_source_flag", "extended-source association flag", ("Xflg", "ext_key")),
    )
    position_uncertainty = PositionUncertainty(
        major_columns=("errMaj", "err_maj"),
        minor_columns=("errMin", "err_min"),
        position_angle_columns=("errPA", "err_ang"),
        scale_to_arcsec=1.0,
    )

    @staticmethod
    def display_source_id(source_id: str) -> str:
        value = " ".join(str(source_id).strip().split())
        upper = value.upper()
        if upper.startswith("2MASS J"):
            return f"2MASS J{value[7:].lstrip()}"
        if upper.startswith("2MASS "):
            value = value[6:].lstrip()
        if value.upper().startswith("J"):
            return f"2MASS {value}"
        return f"2MASS J{value}"

    def acceptance_radius(self, context: CatalogQueryContext) -> float:
        if not context.astrometry.proper_motion_available:
            return self.radius_arcsec
        motion_arcsec_per_year = (
            context.astrometry.pm_ra_cosdec_masyr ** 2
            + context.astrometry.pm_dec_masyr ** 2
        ) ** 0.5 / 1000.0
        return self.radius_arcsec + self.survey_half_span_years * motion_arcsec_per_year

    def __init__(self, backend: str | None = None):
        self.backend = backend

    def selected_backend(self) -> str:
        backend = (self.backend or os.environ.get(TWOMASS_BACKEND_ENV) or "vizier").lower()
        if backend not in TWOMASS_BACKENDS:
            raise ProviderError(
                f"unsupported 2MASS backend {backend!r}; choose one of {sorted(TWOMASS_BACKENDS)}"
            )
        return backend

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
        # Query transport: VizieR is the batch-capable default; IRSA remains a
        # selectable single-cone backend for comparison and service fallback.
        backend = self.selected_backend()
        if backend == "irsa":
            return self._query_irsa(context)
        return self._query_vizier(context)

    def query_many(
        self, contexts: tuple[CatalogQueryContext, ...]
    ) -> dict[int, list[CatalogCandidate]]:
        if self.selected_backend() == "vizier":
            return self.query_many_vizier(contexts)
        return super().query_many(contexts)

    def _query_vizier(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
        return super().query(context)

    def _query_irsa(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
        radius_arcsec = self.query_radius(context)
        acceptance_radius_arcsec = self.acceptance_radius(context)
        coordinate = SkyCoord(
            context.astrometry.ra_deg * u.deg,
            context.astrometry.dec_deg * u.deg,
            frame="icrs",
        )
        try:
            table = Irsa.query_region(
                coordinate,
                catalog=self.irsa_catalog,
                spatial="Cone",
                radius=radius_arcsec * u.arcsec,
            )
        except Exception as error:
            raise ProviderError(
                f"2MASS IRSA query failed: {error}", transient=True
            ) from error
        expected = self.expected_source_ids(context.identifiers)
        return [
            self.annotate_candidate(
                self.parse_row(source_row),
                context=context,
                query_service="IRSA",
                query_catalog=self.irsa_catalog,
                query_radius_arcsec=radius_arcsec,
                acceptance_radius_arcsec=acceptance_radius_arcsec,
                expected=expected,
            )
            for source_row in table
        ]

    @staticmethod
    def candidate_separation(
        context: CatalogQueryContext,
        candidate: CatalogCandidate,
    ) -> float:
        return angular_separation_arcsec(
            context.astrometry,
            candidate.astrometry,
            epoch=candidate.epoch,
        )

    @classmethod
    def parse_row(cls, row) -> CatalogCandidate:
        # Stable catalog identity and native observation position/epoch.
        source_id = row_text(row, "_2MASS", "2MASS", "designation")
        ra = row_float(row, "RAJ2000", "RA_ICRS", "ra")
        dec = row_float(row, "DEJ2000", "DE_ICRS", "dec")
        if not source_id or ra is None or dec is None:
            raise ProviderError("2MASS response omitted identifier or coordinates")
        date = row_text(row, "Date", "xdate")
        try:
            epoch = float(Time(date).jyear) if date else cls.query_epoch
        except (TypeError, ValueError):
            epoch = cls.query_epoch

        # Native per-band photometry, read mode, and quality/exclusion flags.
        qflags = (row_text(row, "Qflg", "ph_qual") or "").ljust(3)
        rflags = (row_text(row, "Rflg", "rd_flg") or "").ljust(3)
        cflags = (row_text(row, "Cflg", "cc_flg") or "").ljust(3)
        measurements = []
        for index, suffix, definition in cls.bands:
            irsa_prefix = ("j", "h", "k")[index]
            magnitude = row_float(row, f"{irsa_prefix}_m", definition.value_column)
            if magnitude is None:
                continue
            read = rflags[index]
            band = f"2MR{read}{suffix}" if read in {"1", "2"} else f"2M{suffix}"
            quality = qflags[index]
            contamination = cflags[index]
            excluded = quality not in {"A", "B", "C", "D"} or contamination != "0"
            measurements.append(
                MeasurementValue(
                    band=band,
                    value=magnitude,
                    error=row_float(
                        row, f"{irsa_prefix}_msigcom", definition.error_column
                    ) or 0.0,
                    systematic_error=definition.systematic_error,
                    unit=definition.unit,
                    bibcode=cls.bibcode,
                    quality=f"{quality}{contamination}",
                    excluded=excluded,
                    exclusion_reason="2MASS quality/contamination flags" if excluded else None,
                    note1=f"Qflg:{quality}",
                    note2=f"Cflg:{contamination}",
                    resolution_major_arcsec=2.5,
                    resolution_minor_arcsec=2.5,
                    resolution_kind="psf_fwhm",
                    resolution_reference="2MASS All-Sky Data Release Explanatory Supplement",
                )
            )
        # Preserve the full provider row and add normalized review metadata.
        payload = add_review_metadata(
            row_payload(row),
            fields=cls.review_fields,
            position_uncertainty=cls.position_uncertainty,
        )
        return CatalogCandidate(source_id, ra, dec, epoch, payload, tuple(measurements))
