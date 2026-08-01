"""Sky-position and pair-geometry policy for hierarchy records."""

from __future__ import annotations

import json
import math

from .wds import UNUSABLE_SEPARATION_ARCSEC
from ..models.hierarchy import HierarchyRecord


def hierarchy_record_positions(
    record: HierarchyRecord,
) -> tuple[tuple[float, float, str], ...]:
    """Return useful base and component-endpoint positions for a record."""
    return record_positions(record)


def record_positions(
    record: HierarchyRecord,
    *,
    raw_payload: dict[str, object] | None = None,
) -> tuple[tuple[float, float, str], ...]:
    if record.ra_deg is None or record.dec_deg is None:
        return ()
    if raw_payload is None:
        raw_payload = record_raw_payload(record)
    values = [(record.ra_deg, record.dec_deg, _record_position_kind(record, raw_payload))]
    if (
        record.separation_arcsec is not None
        and record.pa_deg is not None
        and hierarchy_separation_usable(record.provider, record.separation_arcsec)
    ):
        endpoint = offset_position(
            record.ra_deg, record.dec_deg,
            record.separation_arcsec, record.pa_deg,
        )
        values.append((endpoint[0], endpoint[1], "component endpoint"))
    return tuple(values)


def wds_record_has_unusable_separation(
    record: HierarchyRecord,
    *,
    raw_payload: dict[str, object] | None = None,
) -> bool:
    if record.provider != "wds":
        return False
    if (
        record.separation_arcsec is not None
        and record.separation_arcsec >= UNUSABLE_SEPARATION_ARCSEC
    ):
        return True
    if raw_payload is None:
        raw_payload = record_raw_payload(record)
    return raw_payload.get("unusable_separation_arcsec") is not None


def position_usable_for_matching(position_kind: str) -> bool:
    return position_kind not in {
        "coarse CCDM identifier position",
        "coarse WDS identifier position",
        "low-quality catalog position",
    }


def hierarchy_separation_usable(
    provider: str, separation_arcsec: float | None,
) -> bool:
    if separation_arcsec is None:
        return False
    if provider == "wds" and separation_arcsec >= UNUSABLE_SEPARATION_ARCSEC:
        return False
    return True


def record_raw_payload(record: HierarchyRecord) -> dict[str, object]:
    try:
        value = json.loads(record.raw_payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def offset_position(
    ra_deg: float,
    dec_deg: float,
    separation_arcsec: float,
    pa_deg: float,
) -> tuple[float, float]:
    pa = math.radians(pa_deg)
    east_arcsec = separation_arcsec * math.sin(pa)
    north_arcsec = separation_arcsec * math.cos(pa)
    cos_dec = max(0.01, abs(math.cos(math.radians(dec_deg))))
    return (
        (ra_deg + east_arcsec / (3600.0 * cos_dec)) % 360.0,
        dec_deg + north_arcsec / 3600.0,
    )


def separation_arcsec(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float,
) -> float:
    ra1 = math.radians(ra1_deg)
    dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg)
    dec2 = math.radians(dec2_deg)
    cosine = (
        math.sin(dec1) * math.sin(dec2)
        + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine)))) * 3600.0


def best_separation(current: object, new: float) -> float:
    if current is None:
        return new
    return min(float(current), new)


def _record_position_kind(
    record: HierarchyRecord, raw_payload: dict[str, object],
) -> str:
    coordinate_source = str(raw_payload.get("coordinate_source") or "")
    if coordinate_source == "ccdm_id_only":
        return "coarse CCDM identifier position"
    if coordinate_source == "wds_id_only":
        return "coarse WDS identifier position"
    coo_flag = raw_payload.get("CooFlag")
    try:
        if coo_flag is not None and int(coo_flag) > 0:
            return "low-quality catalog position"
    except (TypeError, ValueError):
        pass
    return "record position"
