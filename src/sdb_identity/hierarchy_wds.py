"""WDS-specific fixed-width parsing and component policy."""

from __future__ import annotations

from .hierarchy_records import (
    ParsedHierarchyRecord,
    coords_from_hierarchy_id,
    delta_mag,
    float_token,
    float_value,
)


UNUSABLE_SEPARATION_ARCSEC = 999.8


def parse_fixed_width(line: str) -> ParsedHierarchyRecord | None:
    if not line.strip() or line.lstrip().startswith(("#", ";")):
        return None
    if dubious_notes(fixed_width_notes(line)):
        return None
    native_id = line[0:10].strip()
    if len(native_id) < 10 or not native_id[:5].isdigit():
        return None
    discoverer = line[10:17].strip() or None
    component = line[17:22].strip() or None
    first_epoch = float_value(line[23:27])
    last_epoch = float_value(line[28:32])
    pa = float_value(line[42:45]) or float_value(line[38:41])
    separation = float_value(line[52:62]) or float_value(line[46:51])
    mag1 = float_value(line[63:68])
    mag2 = float_value(line[69:74])
    if (last_epoch is not None and last_epoch < 1000) or separation is None:
        compact = _parse_compact(line)
        if compact is not None:
            return compact
    return _record(
        line, native_id, discoverer, component, first_epoch, last_epoch,
        pa, separation, mag1, mag2,
    )


def component_pair(component: str | None) -> tuple[str | None, str | None]:
    text = (component or "").strip()
    if not text:
        return None, None
    if "," in text:
        left, right = [part.strip() for part in text.split(",", 1)]
        return left or None, right or None
    compact = text.replace(" ", "")
    if len(compact) == 2 and compact.isalpha():
        return compact[0], compact[1]
    return None, compact or None


def separation_usable(value: float | None) -> bool:
    return value is None or value < UNUSABLE_SEPARATION_ARCSEC


def dubious_notes(value: str | None) -> bool:
    return "X" in (value or "").upper()


def fixed_width_notes(line: str) -> str | None:
    return line[107:].strip() if len(line) > 107 else None


def _parse_compact(line: str) -> ParsedHierarchyRecord | None:
    tokens = line.split()
    if not tokens:
        return None
    native_id = tokens[0][:10]
    if len(native_id) < 10 or not native_id[:5].isdigit():
        return None
    discoverer = tokens[0][10:] or None
    offset = 1
    component = None
    if len(tokens) > offset and any(character.isalpha() for character in tokens[offset]):
        component = tokens[offset]
        offset += 1
    return _record(
        line, native_id, discoverer, component,
        float_token(tokens, offset), float_token(tokens, offset + 1),
        float_token(tokens, offset + 4) or float_token(tokens, offset + 3),
        float_token(tokens, offset + 6) or float_token(tokens, offset + 5),
        float_token(tokens, offset + 7), float_token(tokens, offset + 8),
    )


def _record(
    line: str,
    native_id: str,
    discoverer: str | None,
    component: str | None,
    first_epoch: float | None,
    last_epoch: float | None,
    pa: float | None,
    separation: float | None,
    mag1: float | None,
    mag2: float | None,
) -> ParsedHierarchyRecord:
    ra, dec = coords_from_hierarchy_id(native_id)
    reference, concerned = component_pair(component)
    raw_payload: dict[str, object] = {
        "line": line,
        "rComp": reference or "",
        "Comp": concerned or component or "",
        "component_label": component or "",
        "coordinate_source": "wds_id_only",
    }
    if not separation_usable(separation):
        raw_payload["unusable_separation_arcsec"] = separation
        raw_payload["unusable_separation_reason"] = "WDS 999.9 separation sentinel"
        separation = None
        pa = None
    return ParsedHierarchyRecord(
        native_id=native_id,
        component=component,
        discoverer_id=discoverer,
        ra_deg=ra,
        dec_deg=dec,
        first_epoch=first_epoch,
        last_epoch=last_epoch,
        measure_epoch=last_epoch,
        separation_arcsec=separation,
        pa_deg=pa,
        magnitude_primary=mag1,
        magnitude_secondary=mag2,
        delta_mag=delta_mag(mag1, mag2),
        raw_payload=raw_payload,
    )
