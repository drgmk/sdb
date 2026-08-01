"""Transport and table parsing for hierarchy source snapshots."""

from __future__ import annotations

import csv
import math
import re

from .catalogs.adapters.vizier import row_payload
from .cache_store import CachedSnapshotData
from .hierarchy_records import (
    ParsedHierarchyRecord,
    coords_from_hierarchy_id,
    delta_mag,
    float_value,
)
from .hierarchy_registry import hierarchy_source
from .hierarchy_wds import component_pair, dubious_notes, separation_usable


def parse_snapshot(
    provider: str, text: str,
) -> tuple[list[ParsedHierarchyRecord], int]:
    """Parse a delimited or provider fixed-width hierarchy snapshot."""
    definition = hierarchy_source(provider)
    rows = _parse_delimited_snapshot(provider, text)
    if rows is None:
        rows = [definition.fixed_width_parser(line) for line in text.splitlines()]
    parsed = [row for row in rows if row is not None]
    return parsed, len(rows) - len(parsed)


def parse_tables(provider: str, tables) -> list[ParsedHierarchyRecord]:
    """Parse the configured science tables returned by a snapshot client."""
    parsed = []
    for index, table in enumerate(tables):
        table_name = _astropy_table_name(table, index)
        if not _parseable_table(provider, table_name, getattr(table, "meta", {}) or {}):
            continue
        for row in table:
            record = parse_mapping_record(provider, _string_payload(row_payload(row)))
            if record is not None:
                parsed.append(record)
    return parsed


def parse_cached_snapshot(
    provider: str,
    cached: CachedSnapshotData,
) -> list[ParsedHierarchyRecord]:
    """Parse the configured science tables from a cached snapshot."""
    parsed = []
    for table in cached.tables:
        if not _parseable_table(provider, table.name, table.metadata):
            continue
        for payload in table.rows:
            record = parse_mapping_record(provider, _string_payload(payload))
            if record is not None:
                parsed.append(record)
    return parsed


def parse_mapping_record(
    provider: str, row: dict[str, str],
) -> ParsedHierarchyRecord | None:
    """Normalize one mapping row from WDS or CCDM."""
    lowered = {
        key.lower().strip(): value.strip()
        for key, value in row.items()
        if key is not None
    }
    if provider == "wds" and dubious_notes(_first_text(
        lowered, "notes", "note", "n_notes", "n", "rem", "remarks",
    )):
        return None
    native_id = _first_text(
        lowered, "wds", "wdsid", "ccdm", "id", "name", "native_id",
    )
    if provider == "ccdm" and native_id and native_id.upper().startswith("CCDM "):
        native_id = native_id[5:].strip()
    if not native_id:
        return None
    component = _first_text(lowered, "comp", "component", "components", "m_ccdm")
    discoverer = _first_text(lowered, "disc", "discov", "discoverer", "discoverer_id")
    ra = _first_ra_deg(
        lowered, "ra_deg", "raj2000", "_raj2000", "ra_icrs", "ra", "_ra.icrs",
    )
    dec = _first_dec_deg(
        lowered, "dec_deg", "dej2000", "_dej2000", "de_icrs", "dec", "de", "_de.icrs",
    )
    explicit_position = ra is not None and dec is not None
    if ra is None or dec is None:
        ra, dec = coords_from_hierarchy_id(native_id)
    first_epoch = _first_float(lowered, "first", "first_epoch", "obs1", "date1", "ep1")
    last_epoch = _first_float(lowered, "last", "last_epoch", "obs2", "date2", "ep2")
    measure_epoch = _first_float(lowered, "epoch", "measure_epoch", "last_epoch") or last_epoch
    if provider == "wds":
        separation = _first_float(
            lowered, "sep2", "rho2", "lastsep", "sep", "rho",
            "separation", "separation_arcsec",
        )
        pa = _first_float(
            lowered, "pa2", "theta2", "lastpa", "pa", "theta", "posang", "pa_deg",
        )
    else:
        separation = _first_float(
            lowered, "sep", "sep2", "rho", "separation", "separation_arcsec",
        )
        pa = _first_float(lowered, "pa", "pa2", "theta", "posang", "pa_deg")
    raw_payload: dict[str, object] = dict(row)
    if provider == "wds" and not separation_usable(separation):
        if separation is not None:
            raw_payload["unusable_separation_arcsec"] = separation
            raw_payload["unusable_separation_reason"] = "WDS 999.9 separation sentinel"
        separation = None
        pa = None
    mag1 = _first_float(lowered, "mag1", "m1", "magra", "v1")
    mag2 = _first_float(lowered, "mag2", "m2", "magb", "v2")
    difference = _first_float(lowered, "dmag", "delta_mag")
    if difference is None:
        difference = delta_mag(mag1, mag2)
    if provider == "wds":
        reference_component, concerned_component = component_pair(component)
        raw_payload.update({
            "rComp": reference_component or "",
            "Comp": concerned_component or component or "",
            "component_label": component or "",
            "coordinate_source": "wds_catalog" if explicit_position else "wds_id_only",
        })
    return ParsedHierarchyRecord(
        native_id=native_id,
        component=component,
        discoverer_id=discoverer,
        ra_deg=ra,
        dec_deg=dec,
        first_epoch=first_epoch,
        last_epoch=last_epoch,
        measure_epoch=measure_epoch,
        separation_arcsec=separation,
        pa_deg=pa,
        magnitude_primary=mag1,
        magnitude_secondary=mag2,
        delta_mag=difference,
        raw_payload=raw_payload,
    )


def _string_payload(payload: dict[object, object]) -> dict[str, str]:
    return {
        str(key): "" if value is None else str(value)
        for key, value in payload.items()
    }


def _astropy_table_name(table, index: int) -> str:
    meta = getattr(table, "meta", {}) or {}
    return str(meta.get("name") or meta.get("ID") or f"table{index + 1}")


def _parseable_table(
    provider: str,
    table_name: str,
    metadata: dict[str, object] | None = None,
) -> bool:
    allowed = hierarchy_source(provider).main_table_aliases
    names = {
        table_name.strip().lower(),
        str((metadata or {}).get("name") or "").strip().lower(),
        str((metadata or {}).get("ID") or "").strip().lower(),
    }
    return any(name in allowed for name in names)


def _parse_delimited_snapshot(
    provider: str, text: str,
) -> list[ParsedHierarchyRecord | None] | None:
    sample_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]
    if not sample_lines:
        return []
    delimiter = "\t" if "\t" in sample_lines[0] else ("," if "," in sample_lines[0] else None)
    if delimiter is None:
        return None
    reader = csv.DictReader(sample_lines, delimiter=delimiter)
    if not reader.fieldnames:
        return []
    return [parse_mapping_record(provider, row) for row in reader]


def _first_text(row: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key.lower())
        if value:
            return value
    return None


def _first_float(row: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        value = float_value(row.get(key.lower(), ""))
        if value is not None:
            return value
    return None


def _first_ra_deg(row: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        text = row.get(key.lower(), "")
        value = float_value(text)
        if value is not None:
            return value
        value = _ra_text_to_deg(text)
        if value is not None:
            return value
    return None


def _first_dec_deg(row: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        text = row.get(key.lower(), "")
        value = float_value(text)
        if value is not None:
            return value
        value = _dec_text_to_deg(text)
        if value is not None:
            return value
    return None


def _ra_text_to_deg(value: object) -> float | None:
    parts = _sexagesimal_parts(value)
    if parts is None:
        return None
    hours, minutes, seconds = parts
    ra_deg = (abs(hours) + minutes / 60.0 + seconds / 3600.0) * 15.0
    return ra_deg % 360.0 if math.isfinite(ra_deg) else None


def _dec_text_to_deg(value: object) -> float | None:
    parts = _sexagesimal_parts(value)
    if parts is None:
        return None
    degrees, minutes, seconds = parts
    sign = -1.0 if str(value).strip().startswith("-") or degrees < 0 else 1.0
    dec_deg = sign * (abs(degrees) + minutes / 60.0 + seconds / 3600.0)
    return dec_deg if math.isfinite(dec_deg) else None


def _sexagesimal_parts(value: object) -> tuple[float, float, float] | None:
    text = str(value).strip()
    if not text or ":" not in text and " " not in text:
        return None
    tokens = [token for token in re.split(r"[:\s]+", text) if token]
    if len(tokens) < 3:
        return None
    try:
        return float(tokens[0]), abs(float(tokens[1])), abs(float(tokens[2]))
    except ValueError:
        return None
