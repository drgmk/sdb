from __future__ import annotations

import json
import math

import astropy.units as u
from astropy.coordinates import SkyCoord
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .results import effective_catalog_results
from ..models.catalogs import (
    CatalogDetection,
    CatalogRun,
    IrasBandSelection,
    IrasDetectionFamily,
    IrasSourceFamily,
    NormalizedMeasurement,
    RawCatalogRow,
)
from ..vocabulary import ProviderRunStatus


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


def _iras_identifier(value: str) -> str:
    normalized = str(value).strip().upper()
    if normalized.startswith("IRAS "):
        normalized = normalized[5:].strip()
    return normalized


def _native_psc_identifiers_from_payload(
    payload: dict[str, object],
) -> tuple[str, ...]:
    rows = payload.get("_sdb_native_identifiers", ())
    return tuple(dict.fromkeys(
        _iras_identifier(str(row["identifier"]))
        for row in rows
        if (
            isinstance(row, dict)
            and row.get("relationship") == "iras_fsc_to_psc"
            and row.get("identifier")
        )
    ))


def _native_psc_identifiers(fsc: RawCatalogRow) -> tuple[str, ...]:
    try:
        payload = json.loads(fsc.payload_json)
    except (TypeError, json.JSONDecodeError):
        return ()
    return _native_psc_identifiers_from_payload(payload)


def linked_iras_detection_encounters(
    session: Session,
    detection_id: int,
) -> list[tuple[CatalogDetection, RawCatalogRow, CatalogRun]]:
    """Return the counterpart encounter for a confident PSC/FSC family.

    Stable source families include native and confident ellipse associations.
    One-to-many native links deliberately return no counterpart: those remain
    review cases and must not make one action affect several sources.
    """
    detection = session.get(CatalogDetection, detection_id)
    if detection is None or detection.provider not in IRAS_PROVIDERS:
        return []
    source_family = iras_source_family_for_detection(session, detection.id)
    if source_family is not None:
        counterpart_id = (
            source_family.fsc_detection_id
            if detection.id == source_family.psc_detection_id
            else source_family.psc_detection_id
        )
        counterpart = session.get(CatalogDetection, counterpart_id)
        linked = [] if counterpart is None else [counterpart]
    else:
        candidates = list(session.scalars(select(CatalogDetection).where(
            CatalogDetection.provider.in_(IRAS_PROVIDERS),
            CatalogDetection.id != detection.id,
        )))
        linked = []
    if source_family is None and detection.provider == "iras_fsc":
        try:
            payload = json.loads(detection.payload_json)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        native_ids = _native_psc_identifiers_from_payload(payload)
        if len(native_ids) != 1:
            return []
        linked = [
            value for value in candidates
            if value.provider == "iras_psc"
            and _iras_identifier(value.source_id) == native_ids[0]
        ]
    elif source_family is None:
        psc_id = _iras_identifier(detection.source_id)
        for value in candidates:
            if value.provider != "iras_fsc":
                continue
            try:
                payload = json.loads(value.payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            native_ids = _native_psc_identifiers_from_payload(payload)
            if native_ids == (psc_id,):
                linked.append(value)
    if len(linked) != 1:
        return []
    counterpart = linked[0]
    encounter = session.execute(
        select(RawCatalogRow, CatalogRun)
        .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
        .where(
            RawCatalogRow.detection_id == counterpart.id,
            CatalogRun.is_current.is_(True),
        )
        .order_by(
            RawCatalogRow.accepted.desc(),
            RawCatalogRow.score.desc(),
            RawCatalogRow.id.desc(),
        )
        .limit(1)
    ).one_or_none()
    if encounter is None:
        return []
    raw, run = encounter
    return [(counterpart, raw, run)]


def iras_source_family_for_detection(
    session: Session, detection_id: int,
) -> IrasSourceFamily | None:
    """Return the stable PSC/FSC family containing a detection, if any."""
    return session.scalar(select(IrasSourceFamily).where(
        (IrasSourceFamily.psc_detection_id == detection_id)
        | (IrasSourceFamily.fsc_detection_id == detection_id)
    ))


def _family_association(
    psc: RawCatalogRow,
    fsc: RawCatalogRow,
    normalized_separation: float | None,
) -> tuple[bool, str, str]:
    psc_id = _iras_identifier(psc.source_id)
    fsc_id = _iras_identifier(fsc.source_id)
    native_psc_ids = _native_psc_identifiers(fsc)
    if native_psc_ids:
        if psc_id not in native_psc_ids:
            return False, "native_conflict", (
                "FSC published PSC association disagrees with the selected "
                f"PSC source ({', '.join(native_psc_ids)} versus {psc_id})"
            )
        if len(native_psc_ids) > 1:
            return False, "native_ambiguous", (
                "FSC has more than one published PSC association; operator "
                f"review is required ({', '.join(native_psc_ids)})"
            )
        return True, "fsc_catalogue_42", (
            "FSC association table catalogue 42 explicitly links this source "
            f"to PSC {psc_id}"
        )
    same_designation = fsc_id.removeprefix("F") == psc_id
    within_ellipse = (
        normalized_separation is not None
        and normalized_separation <= 3.0
    )
    if same_designation and within_ellipse:
        return True, "designation_and_ellipse", (
            "PSC/FSC designations correspond and positions agree within the "
            "combined 3-sigma ellipse (native FSC association unavailable)"
        )
    if within_ellipse:
        return True, "combined_ellipse", (
            "PSC/FSC positions agree within the combined 3-sigma ellipse "
            "(native FSC association unavailable)"
        )
    return False, "unassociated", (
        "PSC/FSC sources lack a published cross-identification and do not "
        "have a confident combined-ellipse association"
    )


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
    session.execute(update(IrasDetectionFamily).where(
        IrasDetectionFamily.target_id == target_id,
        *(
            ()
            if existing is None
            else (IrasDetectionFamily.id != existing.id,)
        ),
        IrasDetectionFamily.is_current.is_(True),
    ).values(is_current=False))
    psc_raw = psc_result.selected_raw_row
    fsc_raw = fsc_result.selected_raw_row
    normalized = _normalized_separation(psc_raw, fsc_raw)
    associated, association_method, reason = _family_association(
        psc_raw, fsc_raw, normalized,
    )
    source_family = None
    if associated:
        psc_detection_id = psc_result.selected_detection.id
        fsc_detection_id = fsc_result.selected_detection.id
        source_family = session.scalar(select(IrasSourceFamily).where(
            IrasSourceFamily.psc_detection_id == psc_detection_id,
            IrasSourceFamily.fsc_detection_id == fsc_detection_id,
        ))
        other_family_conditions = (
            ()
            if source_family is None
            else (IrasSourceFamily.id != source_family.id,)
        )
        conflicting = session.scalar(
            select(IrasSourceFamily).where(
                (
                    (IrasSourceFamily.psc_detection_id == psc_detection_id)
                    | (IrasSourceFamily.fsc_detection_id == fsc_detection_id)
                ),
                *other_family_conditions,
            )
        )
        if conflicting is not None:
            associated = False
            association_method = "family_conflict"
            reason = (
                "one detection is already a member of another IRAS source "
                "family; operator review is required"
            )
            source_family = None
        elif source_family is None:
            source_family = IrasSourceFamily(
                psc_detection_id=psc_detection_id,
                fsc_detection_id=fsc_detection_id,
                method=association_method,
                normalized_separation=normalized,
                reason=reason,
            )
            session.add(source_family)
            session.flush()
        else:
            source_family.method = association_method
            source_family.normalized_separation = normalized
            source_family.reason = reason
    if existing is None:
        family = IrasDetectionFamily(
            target_id=target_id,
            psc_run_id=psc_run.id,
            fsc_run_id=fsc_run.id,
            source_family_id=(None if source_family is None else source_family.id),
            status="associated" if associated else "review",
            normalized_separation=normalized,
            reason=reason,
            is_current=True,
        )
        session.add(family)
    else:
        family = existing
        family.target_id = target_id
        family.status = "associated" if associated else "review"
        family.source_family_id = None if source_family is None else source_family.id
        family.normalized_separation = normalized
        family.reason = reason
        family.is_current = True
    session.flush()
    if not associated:
        return family

    session.execute(delete(IrasBandSelection).where(
        IrasBandSelection.family_id == source_family.id,
    ))
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
            family_id=source_family.id,
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
