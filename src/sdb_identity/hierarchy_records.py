"""Transport-neutral hierarchy record values and parsing utilities."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ParsedHierarchyRecord:
    native_id: str
    component: str | None = None
    discoverer_id: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    first_epoch: float | None = None
    last_epoch: float | None = None
    measure_epoch: float | None = None
    separation_arcsec: float | None = None
    pa_deg: float | None = None
    magnitude_primary: float | None = None
    magnitude_secondary: float | None = None
    delta_mag: float | None = None
    raw_payload: dict[str, object] | None = None


def coords_from_hierarchy_id(value: str) -> tuple[float | None, float | None]:
    text = value.strip()
    if text.upper().startswith("J"):
        text = text[1:]
    sign_index = max(text.find("+"), text.find("-"))
    if sign_index < 0:
        return None, None
    ra_text = text[:sign_index]
    dec_text = text[sign_index:]
    sign = -1.0 if dec_text.startswith("-") else 1.0
    dec_body = dec_text[1:]
    try:
        if len(ra_text) < 5 or len(dec_body) < 4:
            return None, None
        hours = int(ra_text[0:2])
        minutes = float(ra_text[2:]) / (10 ** max(0, len(ra_text[2:]) - 2))
        degrees = int(dec_body[0:2])
        arcmin = float(dec_body[2:4])
    except ValueError:
        return None, None
    ra_deg = (hours + minutes / 60.0) * 15.0
    dec_deg = sign * (degrees + arcmin / 60.0)
    if not math.isfinite(ra_deg) or not math.isfinite(dec_deg):
        return None, None
    return ra_deg % 360.0, dec_deg


def float_value(value: object) -> float | None:
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or text in {".", "-", "--"}:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def float_token(tokens: list[str], index: int) -> float | None:
    if index < 0 or index >= len(tokens):
        return None
    return float_value(tokens[index])


def delta_mag(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    return round(second - first, 6)
