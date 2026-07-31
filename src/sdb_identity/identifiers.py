"""Identifier normalization shared by identity, catalog, and target lookup."""

from __future__ import annotations


def normalize_identifier(value: str) -> str:
    """Return the case-insensitive lookup form stored in external identifiers."""

    return " ".join(str(value).upper().split())
