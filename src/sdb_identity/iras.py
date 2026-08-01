from __future__ import annotations

import json
import math

import astropy.units as u
from astropy.coordinates import SkyCoord
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .catalog_results import effective_catalog_results
from .models.catalogs import (
    IrasBandSelection,
    IrasDetectionFamily,
    NormalizedMeasurement,
    RawCatalogRow,
)
from .vocabulary import ProviderRunStatus


IRAS_PROVIDERS = ("iras_psc", "iras_fsc")


def _covariance(payload: dict[str, object]):
    try:
        major = float(payload["Major"])
        minor = float(payload["Minor"])
        angle = math.radians(float(payload["PosAng"]))
    except (KeyError, TypeError, ValueError):
        return None
    if major <= 0 or minor <= 0:
        return None
    sin_angle = math.sin(angle)
    cos_angle = math.cos(angle)
    return (
        major * major * sin_angle * sin_angle + minor * minor * cos_angle * cos_angle,
        (major * major - minor * minor) * sin_angle * cos_angle,
        major * major * cos_angle * cos_angle + minor * minor * sin_angle * sin_angle,
    )


def _normalized_separation(psc: RawCatalogRow, fsc: RawCatalogRow) -> float | None:
    psc_covariance = _covariance(json.loads(psc.payload_json))
    fsc_covariance = _covariance(json.loads(fsc.payload_json))
    if psc_covariance is None or fsc_covariance is None:
        return None
    psc_coord = SkyCoord(psc.ra_deg * u.deg, psc.dec_deg * u.deg)
    fsc_coord = SkyCoord(fsc.ra_deg * u.deg, fsc.dec_deg * u.deg)
    east, north = psc_coord.spherical_offsets_to(fsc_coord)
    x = east.to_value(u.arcsec)
    y = north.to_value(u.arcsec)
    cxx = psc_covariance[0] + fsc_covariance[0]
    cxy = psc_covariance[1] + fsc_covariance[1]
    cyy = psc_covariance[2] + fsc_covariance[2]
    determinant = cxx * cyy - cxy * cxy
    if determinant <= 0:
        return None
    squared = (cyy * x * x - 2.0 * cxy * x * y + cxx * y * y) / determinant
    return math.sqrt(max(0.0, squared))


def _preference(value: NormalizedMeasurement):
    try:
        quality = int(value.quality or 0)
    except ValueError:
        quality = 0
    return (not value.upper_limit, quality, value.provider == "iras_psc")


def reconcile_iras_target(session: Session, target_id: int) -> IrasDetectionFamily | None:
    results = {
        result.provider: result
        for result in effective_catalog_results(
            session, [target_id], providers=IRAS_PROVIDERS,
        ).values()
        if result.status == ProviderRunStatus.MATCH
    }
    if set(results) != set(IRAS_PROVIDERS):
        session.execute(update(IrasDetectionFamily).where(
            IrasDetectionFamily.target_id == target_id,
            IrasDetectionFamily.is_current.is_(True),
        ).values(is_current=False))
        return None

    psc_result = results["iras_psc"]
    fsc_result = results["iras_fsc"]
    psc_run = psc_result.run
    fsc_run = fsc_result.run
    existing = session.scalar(select(IrasDetectionFamily).where(
        IrasDetectionFamily.psc_run_id == psc_run.id,
        IrasDetectionFamily.fsc_run_id == fsc_run.id,
    ))
    if existing is not None:
        session.execute(update(IrasDetectionFamily).where(
            IrasDetectionFamily.target_id == target_id,
            IrasDetectionFamily.id != existing.id,
            IrasDetectionFamily.is_current.is_(True),
        ).values(is_current=False))
        existing.is_current = True
        return existing

    session.execute(update(IrasDetectionFamily).where(
        IrasDetectionFamily.target_id == target_id,
        IrasDetectionFamily.is_current.is_(True),
    ).values(is_current=False))
    psc_raw = psc_result.selected_raw_row
    fsc_raw = fsc_result.selected_raw_row
    normalized = _normalized_separation(psc_raw, fsc_raw)
    associated = normalized is not None and normalized <= 3.0
    family = IrasDetectionFamily(
        target_id=target_id,
        psc_run_id=psc_run.id,
        fsc_run_id=fsc_run.id,
        status="associated" if associated else "review",
        normalized_separation=normalized,
        reason=(
            "PSC/FSC positions agree within the combined 3-sigma ellipse"
            if associated else
            "PSC/FSC positions lack a confident combined-ellipse association"
        ),
        is_current=True,
    )
    session.add(family)
    session.flush()
    if not associated:
        return family

    measurements = list(session.scalars(
        select(NormalizedMeasurement)
        .where(
            NormalizedMeasurement.detection_id.in_((
                psc_result.selected_detection.id,
                fsc_result.selected_detection.id,
            )),
        )
    ))
    by_band: dict[str, list[NormalizedMeasurement]] = {}
    for value in measurements:
        by_band.setdefault(value.band, []).append(value)
    for band, values in by_band.items():
        if len(values) != 2:
            continue
        selected = max(values, key=_preference)
        alternate = next(value for value in values if value.id != selected.id)
        session.add(IrasBandSelection(
            family_id=family.id,
            band=band,
            selected_measurement_id=selected.id,
            alternate_measurement_id=alternate.id,
            method="quality_then_psc",
            reason=(
                "detection preferred over upper limit; then higher flux quality; "
                "PSC preferred on ties"
            ),
        ))
    return family
