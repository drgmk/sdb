from __future__ import annotations

import math
import os

import astropy.units as u
from astropy.coordinates import SkyCoord
from astroquery.ipac.irsa.core import IrsaClass
import requests

from ..registry import catalog_provider
from ..types import (
    CatalogAttributeValue,
    CatalogCandidate,
    CatalogQueryContext,
    MeasurementValue,
)
from ...providers import ProviderError
from .vizier import (
    BandDefinition,
    VizierConeAdapter,
    row_float,
    row_payload,
    row_text,
)
from .review_metadata import add_review_metadata, PositionUncertainty, ReviewField


ALLWISE_BACKEND_ENV = "SDB_ALLWISE_BACKEND"
ALLWISE_BACKENDS = {"vizier", "irsa"}
_PROVIDER = catalog_provider("allwise")


class AllWiseAdapter(VizierConeAdapter):
    # Catalog identity and query policy.
    name = _PROVIDER.key
    display_name = _PROVIDER.display_name
    release = _PROVIDER.catalog
    irsa_catalog = "allwise_p3as_psd"
    # AllWISE combines observations over roughly one year in 2010. Treat it as
    # an epoch-local positional catalog; its fitted PM is not used for matching.
    query_epoch = _PROVIDER.query_epoch
    radius_arcsec = _PROVIDER.radius_arcsec
    review_radius_arcsec = _PROVIDER.review_radius_arcsec
    bibcode = _PROVIDER.bibliography
    source_id_columns = ("AllWISE",)
    # SIMBAD generally exposes AllWISE sources as WISEA J... identifiers.
    # This is declared here because the convention is catalog-specific.
    identifier_prefixes = ("WISEA ", "AllWISE ")
    bands = (
        (0, BandDefinition("WISE3P4", "W1mag", "e_W1mag", 0.024), 4.5),
        (1, BandDefinition("WISE4P6", "W2mag", "e_W2mag", 0.028), 4.0),
        (2, BandDefinition("WISE12", "W3mag", "e_W3mag", 0.045), 0.0),
        (3, BandDefinition("WISE22", "W4mag", "e_W4mag", 0.057), 0.0),
    )
    band_wavelengths_micron = _PROVIDER.bands
    resolution_arcsec = (6.1, 6.4, 6.5, 12.0)
    timeout_seconds = 30.0
    # Provider columns exposed directly to match review.
    review_fields = (
        ReviewField(
            "simultaneous_psf_components", "simultaneous PSF components",
            ("nb",), neighbourhood=True,
        ),
        ReviewField(
            "active_deblend", "active deblend flag", ("na",),
            neighbourhood=True,
        ),
        ReviewField("contamination_flags", "contamination flags", ("ccf", "cc_flags")),
        ReviewField("photometry_quality", "photometry quality", ("qph", "ph_qual")),
        ReviewField("extended_source_flag", "extended-source flag", ("ex", "ext_flg")),
    )
    position_uncertainty = (
        PositionUncertainty(
            major_columns=("eeMaj",),
            minor_columns=("eeMin",),
            position_angle_columns=("eePA",),
            scale_to_arcsec=1.0,
        ),
        PositionUncertainty(
            major_columns=("sigra",),
            minor_columns=("sigdec",),
            scale_to_arcsec=1.0,
            kind="coordinate_errors",
        ),
    )

    @staticmethod
    def display_source_id(source_id: str) -> str:
        value = " ".join(str(source_id).strip().split())
        upper = value.upper()
        for prefix in ("ALLWISE ", "WISEA "):
            if upper.startswith(prefix):
                value = value[len(prefix):].lstrip()
                break
        if not value.upper().startswith("J"):
            value = f"J{value}"
        return f"AllWISE {value}"

    class _TimeoutSession(requests.Session):
        def __init__(self, timeout_seconds: float):
            super().__init__()
            self.timeout_seconds = timeout_seconds

        def request(self, method, url, **kwargs):
            kwargs.setdefault("timeout", self.timeout_seconds)
            return super().request(method, url, **kwargs)

    def _query_region(self, coordinate: SkyCoord, radius_arcsec: float):
        # Astroquery's module-level IRSA client is shared and PyVO does not
        # supply a request timeout. A per-call client is safe under bounded
        # workers, while this session bounds every HTTP request made by PyVO.
        client = IrsaClass()
        client._session = self._TimeoutSession(self.timeout_seconds)
        return client.query_region(
            coordinate,
            catalog=self.irsa_catalog,
            spatial="Cone",
            radius=radius_arcsec * u.arcsec,
        )

    def acceptance_radius(self, context: CatalogQueryContext) -> float:
        if not context.astrometry.proper_motion_available:
            return self.radius_arcsec
        motion_arcsec_per_year = math.hypot(
            context.astrometry.pm_ra_cosdec_masyr,
            context.astrometry.pm_dec_masyr,
        ) / 1000.0
        return self.radius_arcsec + 0.5 * motion_arcsec_per_year

    def __init__(self, backend: str | None = None):
        self.backend = backend

    def selected_backend(self) -> str:
        backend = (self.backend or os.environ.get(ALLWISE_BACKEND_ENV) or "vizier").lower()
        if backend not in ALLWISE_BACKENDS:
            raise ProviderError(
                f"unsupported AllWISE backend {backend!r}; choose one of {sorted(ALLWISE_BACKENDS)}"
            )
        return backend

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
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
            table = self._query_region(coordinate, radius_arcsec)
        except Exception as error:
            raise ProviderError(
                f"AllWISE IRSA query failed: {error}", transient=True
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

    @classmethod
    def parse_row(cls, row) -> CatalogCandidate:
        # Stable catalog identity and native epoch-2010.5 position.
        source_id = row_text(row, "AllWISE", "designation")
        ra = row_float(row, "RAJ2000", "ra")
        dec = row_float(row, "DEJ2000", "dec")
        if not source_id or ra is None or dec is None:
            raise ProviderError(
                "AllWISE response omitted identifier or coordinates"
            )
        # Native per-band photometry and quality/exclusion flags.
        qflags = (row_text(row, "qph", "ph_qual") or "").ljust(4)
        cflags = (row_text(row, "ccf", "cc_flags") or "").ljust(4)
        variability = row_text(row, "var", "var_flg") or ""
        measurements = []
        for index, definition, bright_limit in cls.bands:
            irsa_band = f"w{index + 1}"
            magnitude = row_float(row, f"{irsa_band}mpro", definition.value_column)
            if magnitude is None:
                continue
            quality = qflags[index].upper()
            contamination = cflags[index].upper()
            reasons = []
            if contamination in {"H", "P", "D"}:
                reasons.append(f"ccf={contamination}")
            if quality == "Z":
                reasons.append("qph=Z")
            if magnitude < bright_limit:
                reasons.append(f"magnitude<{bright_limit:g}")
            measurements.append(
                MeasurementValue(
                    band=definition.band,
                    value=magnitude,
                    error=row_float(
                        row, f"{irsa_band}sigmpro", definition.error_column
                    ) or 0.0,
                    systematic_error=definition.systematic_error,
                    unit=definition.unit,
                    bibcode=cls.bibcode,
                    quality=f"{quality}{contamination}",
                    upper_limit=quality == "U",
                    excluded=bool(reasons),
                    exclusion_reason="AllWISE " + ", ".join(reasons) if reasons else None,
                    note1=f"qual:{quality}",
                    note2=f"Var:{variability}",
                    resolution_major_arcsec=cls.resolution_arcsec[index],
                    resolution_minor_arcsec=cls.resolution_arcsec[index],
                    resolution_kind="psf_fwhm",
                    resolution_reference="AllWISE Explanatory Supplement",
                )
            )
        # Auxiliary apparent-motion fit. These short-baseline values are not
        # stellar proper motion: keep them inspectable, but use names which
        # cannot be consumed by the generic PM propagation path.
        pmcode = row_text(row, "pmcode", "qpm")
        attributes = []
        for key, value_columns, error_columns in (
            (
                "apparent_motion_ra_cosdec",
                ("pmra", "pmRA"),
                ("sigpmra", "e_pmRA"),
            ),
            (
                "apparent_motion_dec",
                ("pmdec", "pmDE"),
                ("sigpmdec", "e_pmDE"),
            ),
        ):
            value = row_float(row, *value_columns)
            if value is not None:
                attributes.append(CatalogAttributeValue(
                    key=key,
                    value_float=value,
                    uncertainty=row_float(row, *error_columns),
                    unit="mas/yr",
                    quality=pmcode,
                    reference="AllWISE Source Catalog apparent-motion fit",
                    note=(
                        "short-baseline apparent motion retained as provider "
                        "metadata; not proper motion and never used for "
                        "association or coordinate propagation"
                    ),
                ))
        # Preserve the complete provider row, augmented with normalized review
        # fields and positional uncertainty.
        payload = add_review_metadata(
            row_payload(row),
            fields=cls.review_fields,
            position_uncertainty=cls.position_uncertainty,
        )
        return CatalogCandidate(
            source_id=source_id,
            ra_deg=ra,
            dec_deg=dec,
            epoch=cls.query_epoch,
            payload=payload,
            measurements=tuple(measurements),
            attributes=tuple(attributes),
        )
