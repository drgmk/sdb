"""Shared JSON serialization helpers for snapshot ingestion.

These convert astropy/masked table cells and numpy scalars into plain JSON-safe
Python values. They are duck-typed (no astropy import) so the lightweight cache
store can reuse them without pulling in heavy provider dependencies.
"""

from __future__ import annotations

import json
import math
from typing import Any


def json_default(value):
    """Fallback serializer for `json.dumps` (numpy scalars, bytes, etc.)."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def safe_json(value) -> str:
    """Deterministic JSON encoding tolerant of numpy/astropy scalar cells."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=json_default)


def json_value(value):
    """Convert a single (possibly masked) table cell to a JSON-safe value."""
    mask = getattr(value, "mask", False)
    try:
        masked = bool(mask) if not hasattr(mask, "any") else bool(mask.any())
    except ValueError:
        masked = True
    if masked:
        return None
    if hasattr(value, "item") and getattr(value, "ndim", 0) == 0:
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def row_payload(row) -> dict[str, object]:
    """Convert an astropy Row (or dict) into a JSON-safe dict."""
    names = row.keys() if isinstance(row, dict) else row.colnames
    return {str(name): json_value(row[name]) for name in names}


def row_value(row: Any, *names: str):
    """Read the first present, unmasked column using case-insensitive names."""

    columns = {str(name).lower(): name for name in getattr(row, "colnames", ())}
    if isinstance(row, dict):
        columns.update({str(name).lower(): name for name in row})
    for name in names:
        key = columns.get(name.lower())
        if key is None:
            continue
        value = row[key]
        mask = getattr(value, "mask", False)
        try:
            masked = bool(mask) if not hasattr(mask, "any") else bool(mask.any())
        except ValueError:
            masked = True
        if masked or value is None:
            continue
        return (
            value.item()
            if hasattr(value, "item") and getattr(value, "ndim", 0) == 0
            else value
        )
    return None


def row_text(row: Any, *names: str) -> str | None:
    value = row_value(row, *names)
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return str(value).strip()


def row_float(row: Any, *names: str) -> float | None:
    value = row_value(row, *names)
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None
