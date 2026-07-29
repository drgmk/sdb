"""Provider-native component semantics for the GCPD UBV means catalogue."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from collections.abc import Iterable, Mapping


_SOURCE_COMPONENT = re.compile(r"(?:^|\|)m_LID=([^|]*)", re.IGNORECASE)
_TRAILING_COMPONENT = re.compile(r"(?<=\d)([A-Z])$")


@dataclass(frozen=True)
class UbvComponentSemantics:
    native_code: str
    kind: str
    ordinal: int | None = None
    component_label: str | None = None
    minimum_contributors: int | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def ubv_component_code(
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> str:
    if payload is not None:
        value = payload.get("m_LID")
        if value is not None:
            return str(value).strip().upper()
    matched = _SOURCE_COMPONENT.search(source_id or "")
    return "" if matched is None else matched.group(1).strip().upper()


def decode_ubv_component(
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> UbvComponentSemantics:
    """Decode the catalogue's native Lausanne component field.

    Numeric components are ordinal (1=A, 2=B, ...). ``D`` records combined
    light from at least two components but does not identify which subset.
    ``S`` is a supplementary identification flag rather than a component.
    """
    code = ubv_component_code(payload, source_id)
    if not code:
        return UbvComponentSemantics("", "unspecified")
    if code.isdigit() and 1 <= int(code) <= 26:
        ordinal = int(code)
        return UbvComponentSemantics(
            code,
            "component_ordinal",
            ordinal=ordinal,
            component_label=chr(ord("A") + ordinal - 1),
        )
    if code == "D":
        return UbvComponentSemantics(
            code,
            "combined_components",
            minimum_contributors=2,
        )
    if code == "S":
        return UbvComponentSemantics(code, "supplementary_identifier")
    return UbvComponentSemantics(code, "unknown")


def ubv_component_identifiers(
    identifiers: Iterable[str],
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> tuple[str, ...]:
    """Apply an ordinal component suffix to UBV identifiers when specified.

    The unsuffixed catalogue name is intentionally not retained for numeric
    rows: it commonly names the system and would otherwise be mistaken for
    exact identity evidence for a composite target.
    """
    values = tuple(str(value).strip() for value in identifiers if str(value).strip())
    semantics = decode_ubv_component(payload, source_id)
    label = semantics.component_label
    if label is None:
        return values
    result = []
    for value in values:
        matched = _TRAILING_COMPONENT.search(value.upper())
        if matched is not None:
            result.append(value if matched.group(1) == label else f"{value} {label}")
        else:
            result.append(f"{value}{label}")
    return tuple(result)


def ubv_photometry_scope(
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> tuple[str, str, str | None]:
    semantics = decode_ubv_component(payload, source_id)
    if semantics.kind == "combined_components":
        return "system", "blended", "catalog_multiple_in_aperture"
    if semantics.kind in {"supplementary_identifier", "unknown"}:
        return "ambiguous", "unknown", (
            "catalog_supplementary_identifier"
            if semantics.kind == "supplementary_identifier"
            else "unknown_catalog_component_code"
        )
    return "component", "clear", None
