from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
import math

from astropy.table import Row, Table


FIELDS = (
    "Phot", "Err", "Sys", "Lim", "Unit", "bibcode", "Note1", "Note2",
    "SourceID", "private", "exclude",
)
METADATA_FIELDS = (
    "main_id", "raj2000", "dej2000", "sp_type", "sp_bibcode",
    "plx_value", "plx_err", "plx_bibcode", "otype",
)


def _value(value: Any) -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _text(value: Any) -> str:
    value = _value(value)
    return "" if value is None else " ".join(str(value).split()).casefold()


def _number(value: Any) -> float | None:
    value = _value(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _different(field: str, legacy: Any, current: Any) -> bool:
    if field in {
        "Phot", "Err", "Sys", "raj2000", "dej2000", "plx_value", "plx_err",
    }:
        left, right = _number(legacy), _number(current)
        if left is None or right is None:
            return left != right
        return not math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-10)
    return _text(legacy) != _text(current)


def _row(row: Row) -> dict[str, Any]:
    return {"Band": str(row["Band"]).strip(), **{
        field: _value(row[field]) if field in row.colnames else None
        for field in FIELDS
    }}


def _pair_cost(legacy: Row, current: Row) -> float:
    cost = 0.0
    if _text(legacy["SourceID"]) != _text(current["SourceID"]):
        cost += 8
    if _text(legacy["bibcode"]) != _text(current["bibcode"]):
        cost += 3
    if _text(legacy["Unit"]) != _text(current["Unit"]):
        cost += 20
    if _text(legacy["Lim"]) != _text(current["Lim"]):
        cost += 5
    left, right = _number(legacy["Phot"]), _number(current["Phot"])
    if left is None or right is None:
        cost += 10 if left != right else 0
    else:
        scale = max(abs(left), abs(right), 1e-12)
        cost += min(abs(left - right) / scale, 10)
    return cost


def _metadata(table: Table, key: str) -> Any:
    value = table.meta.get("keywords", {}).get(key)
    if isinstance(value, dict):
        value = value.get("value")
    return _value(value)


def compare_exports(legacy_path: str | Path, current_path: str | Path) -> dict[str, Any]:
    """Return a neutral semantic diff between legacy and current IPAC exports."""
    legacy_path, current_path = Path(legacy_path), Path(current_path)
    legacy = Table.read(legacy_path, format="ascii.ipac")
    current = Table.read(current_path, format="ascii.ipac")

    metadata = []
    for field in METADATA_FIELDS:
        left, right = _metadata(legacy, field), _metadata(current, field)
        if _different(field, left, right):
            metadata.append({"field": field, "legacy": left, "current": right})

    legacy_bands: dict[str, list[Row]] = defaultdict(list)
    current_bands: dict[str, list[Row]] = defaultdict(list)
    for row in legacy:
        legacy_bands[str(row["Band"]).strip()].append(row)
    for row in current:
        current_bands[str(row["Band"]).strip()].append(row)

    observations = []
    unchanged = 0
    for band in sorted(set(legacy_bands) | set(current_bands)):
        left = list(legacy_bands[band])
        right = list(current_bands[band])
        while left and right:
            _, left_index, right_index = min(
                (_pair_cost(a, b), i, j)
                for i, a in enumerate(left)
                for j, b in enumerate(right)
            )
            old, new = left.pop(left_index), right.pop(right_index)
            changes = [
                field for field in FIELDS
                if _different(field, old[field], new[field])
            ]
            if changes:
                observations.append({
                    "kind": "changed_row", "band": band,
                    "changed_fields": changes,
                    "legacy": _row(old), "current": _row(new),
                    "review": "unreviewed",
                })
            else:
                unchanged += 1
        observations.extend({
            "kind": "legacy_only_row", "band": band, "legacy": _row(row),
            "current": None, "review": "unreviewed",
        } for row in left)
        observations.extend({
            "kind": "current_only_row", "band": band, "legacy": None,
            "current": _row(row), "review": "unreviewed",
        } for row in right)

    return {
        "legacy": str(legacy_path), "current": str(current_path),
        "legacy_rows": len(legacy), "current_rows": len(current),
        "unchanged_rows": unchanged, "metadata_changes": metadata,
        "observations": observations,
        "review_required": bool(metadata or observations),
    }
