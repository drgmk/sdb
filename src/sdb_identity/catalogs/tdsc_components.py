"""Component identity semantics for the Tycho Double Star Catalogue."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from collections.abc import Iterable, Mapping


_SOURCE_COMPONENT = re.compile(r"(?:^|\|)m_TDSC=([^|]*)", re.IGNORECASE)


@dataclass(frozen=True)
class TdscComponentSemantics:
    native_code: str
    kind: str
    component_label: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def tdsc_component_code(
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> str:
    if payload is not None:
        value = payload.get("m_TDSC")
        if value is not None:
            return str(value).strip()
    matched = _SOURCE_COMPONENT.search(source_id or "")
    return "" if matched is None else matched.group(1).strip()


def decode_tdsc_component(
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> TdscComponentSemantics:
    code = tdsc_component_code(payload, source_id)
    if not code:
        return TdscComponentSemantics("", "unspecified")
    return TdscComponentSemantics(code, "named_component", code)


def tdsc_component_identifiers(
    identifiers: Iterable[str],
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> tuple[str, ...]:
    """Return component-specific identifiers, keeping HIP as system evidence."""
    values = tuple(str(value).strip() for value in identifiers if str(value).strip())
    component = decode_tdsc_component(payload, source_id).component_label
    if not component:
        return values

    hd = []
    tyc = []
    wds = []
    hip = []
    other = []
    for value in values:
        upper = value.upper()
        if upper.startswith("HD "):
            hd.append(_append_component(value, component))
        elif upper.startswith("TYC "):
            tyc.append(value)
        elif upper.startswith("WDS "):
            native = value[4:].strip()
            if not native.upper().startswith("J"):
                native = f"J{native}"
            wds.append(_append_component(f"WDS {native}", component))
        elif upper.startswith("HIP "):
            hip.append(value)
        else:
            other.append(value)
    return tuple((*hd, *tyc, *wds, *other, *hip))


def _append_component(value: str, component: str) -> str:
    return value if value.upper().endswith(component.upper()) else f"{value}{component}"
