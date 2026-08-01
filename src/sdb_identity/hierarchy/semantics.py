"""Public component-label and SIMBAD relationship semantics."""

from __future__ import annotations

import re


_COMPONENT_TOKEN_RE = re.compile(r"^(?:[A-Z]{1,3}|[A-Z][a-z0-9])$")
_TRAILING_COMPONENT_RE = re.compile(r"(?:^|[\s_-])([A-Z]{1,3}|[A-Z][a-z0-9])$")
_WDS_CCDM_COMPONENT_RE = re.compile(
    r"\b(?:WDS|CCDM)\s+J?\d{4,6}[+-]\d{4,6}\s*"
    r"([A-Z]{1,3}|[A-Z][a-z0-9])\b",
    re.IGNORECASE,
)
_HD_ATTACHED_COMPONENT_RE = re.compile(r"^HD\s+\d+([A-Z]{1,3}|[A-Z][a-z0-9])$")

_SIMBAD_PLANETARY_OR_DISK_TYPES = {
    "pl", "pl?", "planet", "exoplanet", "disk", "debrisdisk",
    "debris disk", "protoplanetarydisk", "protoplanetary disk",
}
_SIMBAD_CONTEXTUAL_GROUP_TYPES = {
    "cl*", "assoc*", "as*", "assoc", "association", "mgr",
    "moving group", "cluster", "open cluster", "globular cluster",
    "region", "hii", "molcld", "cloud", "neb", "nebula",
}


def component_label_from_identifier(value: str) -> str | None:
    """Infer an explicit component suffix from a recognized identifier form."""
    text = " ".join(value.strip().split())
    if not text:
        return None
    matched = _WDS_CCDM_COMPONENT_RE.search(text)
    if matched:
        return normalize_component_label(matched.group(1))
    matched = _HD_ATTACHED_COMPONENT_RE.fullmatch(text)
    if matched:
        return normalize_component_label(matched.group(1))
    token = text.rsplit(" ", 1)[-1]
    if _COMPONENT_TOKEN_RE.fullmatch(token) and not _token_looks_like_catalog_suffix(text, token):
        return normalize_component_label(token)
    matched = _TRAILING_COMPONENT_RE.search(text)
    if matched:
        return normalize_component_label(matched.group(1))
    return None


def simbad_component_relevance(
    primary_type: str | None, object_types: list[str],
) -> str:
    """Classify whether a SIMBAD relationship describes a stellar component."""
    codes = {
        value.strip().lower()
        for value in [primary_type, *object_types]
        if value
    }
    if not codes:
        return "unknown"
    if codes & _SIMBAD_PLANETARY_OR_DISK_TYPES:
        return "planetary_or_disk"
    if codes & _SIMBAD_CONTEXTUAL_GROUP_TYPES:
        return "contextual_group"
    if any(_is_stellar_or_substellar(code) for code in codes):
        return "stellar_or_substellar_component"
    return "unknown"


def _token_looks_like_catalog_suffix(text: str, token: str) -> bool:
    if len(token) != 1:
        return False
    prefix = text[: -len(token)].rstrip()
    return bool(prefix and prefix[-1].isdigit() and " " not in prefix)


def normalize_component_label(value: str) -> str:
    if len(value) >= 2 and value[0].isalpha() and value[1:].islower():
        return value[0].upper() + value[1:]
    return value.upper()


def _is_stellar_or_substellar(code: str) -> bool:
    return "*" in code or code in {
        "star", "bd", "bd?", "brown dwarf", "low-mass*",
    }
