from __future__ import annotations

import math
import warnings
from dataclasses import replace

import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time
from erfa import ErfaWarning

from .providers import Astrometry


def validate_position(ra_deg: float, dec_deg: float) -> tuple[float, float]:
    if not math.isfinite(ra_deg) or not math.isfinite(dec_deg):
        raise ValueError("coordinates must be finite")
    if not -90.0 <= dec_deg <= 90.0:
        raise ValueError("declination must be between -90 and 90 degrees")
    return ra_deg % 360.0, dec_deg


def propagate_to_epoch(value: Astrometry, epoch: float = 2000.0) -> Astrometry:
    ra, dec = validate_position(value.ra_deg, value.dec_deg)
    if not value.proper_motion_available or value.epoch == epoch:
        return replace(value, ra_deg=ra, dec_deg=dec, epoch=epoch)

    # Deliberately omit distance and radial velocity. Perspective acceleration is
    # negligible for SDB identity precision, while RV remains stored as metadata.
    coordinate = SkyCoord(
        ra=ra * u.deg,
        dec=dec * u.deg,
        pm_ra_cosdec=value.pm_ra_cosdec_masyr * u.mas / u.yr,
        pm_dec=value.pm_dec_masyr * u.mas / u.yr,
        obstime=Time(value.epoch, format="jyear"),
        frame="icrs",
    )
    # ERFA warns that it substitutes an arbitrary distance. Angular linear
    # propagation is still the desired calculation when distance is unknown.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*distance overridden.*", category=ErfaWarning)
        moved = coordinate.apply_space_motion(new_obstime=Time(epoch, format="jyear"))
    return replace(value, ra_deg=float(moved.ra.deg) % 360.0, dec_deg=float(moved.dec.deg), epoch=epoch)


def angular_separation_arcsec(first: Astrometry, second: Astrometry, *, epoch: float = 2000.0) -> float:
    a = propagate_to_epoch(first, epoch)
    b = propagate_to_epoch(second, epoch)
    ca = SkyCoord(a.ra_deg * u.deg, a.dec_deg * u.deg, frame="icrs")
    cb = SkyCoord(b.ra_deg * u.deg, b.dec_deg * u.deg, frame="icrs")
    return float(ca.separation(cb).arcsec)


def make_sdbid(ra_deg: float, dec_deg: float, *, prefix: str = "sdbid-v3-") -> str:
    ra, dec = validate_position(ra_deg, dec_deg)
    coordinate = SkyCoord(ra * u.deg, dec * u.deg, frame="icrs")
    ra_text = coordinate.ra.to_string(unit=u.hourangle, sep="", precision=2, pad=True)
    dec_text = coordinate.dec.to_string(unit=u.deg, sep="", precision=1, pad=True, alwayssign=True)
    # Rounding just below 360 degrees may produce 24h; the identifier uses 00h.
    if ra_text.startswith("24"):
        ra_text = "00" + ra_text[2:]
    return f"{prefix}{ra_text}{dec_text}"
