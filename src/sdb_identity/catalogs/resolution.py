from __future__ import annotations

from dataclasses import dataclass


ARCSEC_PER_RADIAN = 206264.80624709636


@dataclass(frozen=True)
class CatalogResolution:
    major_arcsec: float
    minor_arcsec: float
    kind: str
    reference: str


@dataclass(frozen=True)
class DiffractionBand:
    wavelength_micron: float
    aperture_m: float
    reference: str

    def resolution(self) -> CatalogResolution:
        arcsec = 1.22 * self.wavelength_micron * 1e-6 / self.aperture_m * ARCSEC_PER_RADIAN
        return CatalogResolution(
            major_arcsec=arcsec,
            minor_arcsec=arcsec,
            kind="diffraction_estimate_1.22_lambda_over_d",
            reference=self.reference,
        )


CATALOG_RESOLUTIONS: dict[str, dict[str, CatalogResolution]] = {
    # 2MASS and AllWISE adapters currently store empirical PSF/FWHM values
    # themselves. They are intentionally omitted here so those values remain
    # explicit catalog policy rather than fallback defaults.
    "gaia_dr3": {
        # Use the along-scan primary dimension as a simple diffraction estimate.
        # This is not the Gaia source-separation selection function.
        "GAIA.G": DiffractionBand(
            0.673,
            1.45,
            "Gaia mission/telescope description; simple 1.22 lambda/D estimate",
        ).resolution(),
        "GAIA.BP": DiffractionBand(
            0.532,
            1.45,
            "Gaia mission/telescope description; simple 1.22 lambda/D estimate",
        ).resolution(),
        "GAIA.RP": DiffractionBand(
            0.797,
            1.45,
            "Gaia mission/telescope description; simple 1.22 lambda/D estimate",
        ).resolution(),
    },
    "tycho2": {
        "BT": DiffractionBand(
            0.43,
            0.29,
            "Hipparcos/Tycho star-mapper telescope aperture; simple 1.22 lambda/D estimate",
        ).resolution(),
        "VT": DiffractionBand(
            0.53,
            0.29,
            "Hipparcos/Tycho star-mapper telescope aperture; simple 1.22 lambda/D estimate",
        ).resolution(),
    },
    "hip2": {
        "HP": DiffractionBand(
            0.52,
            0.29,
            "Hipparcos telescope aperture; simple 1.22 lambda/D estimate",
        ).resolution(),
    },
    "tdsc": {
        "BT": DiffractionBand(
            0.43,
            0.29,
            "Hipparcos/Tycho star-mapper telescope aperture; simple 1.22 lambda/D estimate",
        ).resolution(),
        "VT": DiffractionBand(
            0.53,
            0.29,
            "Hipparcos/Tycho star-mapper telescope aperture; simple 1.22 lambda/D estimate",
        ).resolution(),
    },
    "iras_psc": {
        "IRAS12": CatalogResolution(30.0, 30.0, "catalog_effective_resolution", "IRAS Catalogs and Atlases Explanatory Supplement"),
        "IRAS25": CatalogResolution(30.0, 30.0, "catalog_effective_resolution", "IRAS Catalogs and Atlases Explanatory Supplement"),
        "IRAS60": CatalogResolution(60.0, 60.0, "catalog_effective_resolution", "IRAS Catalogs and Atlases Explanatory Supplement"),
        "IRAS100": CatalogResolution(120.0, 120.0, "catalog_effective_resolution", "IRAS Catalogs and Atlases Explanatory Supplement"),
    },
    "iras_fsc": {
        "IRAS12": CatalogResolution(30.0, 30.0, "catalog_effective_resolution", "IRAS Faint Source Catalog Explanatory Supplement"),
        "IRAS25": CatalogResolution(30.0, 30.0, "catalog_effective_resolution", "IRAS Faint Source Catalog Explanatory Supplement"),
        "IRAS60": CatalogResolution(60.0, 60.0, "catalog_effective_resolution", "IRAS Faint Source Catalog Explanatory Supplement"),
        "IRAS100": CatalogResolution(120.0, 120.0, "catalog_effective_resolution", "IRAS Faint Source Catalog Explanatory Supplement"),
    },
}


def default_resolution(provider: str, band: str) -> CatalogResolution | None:
    return CATALOG_RESOLUTIONS.get(provider, {}).get(band)

