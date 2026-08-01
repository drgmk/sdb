"""Component identity semantics for the Gliese V/70A catalogue."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
import re


_SOURCE_COMPONENT = re.compile(r"(?:^|\|)Comp=([^|]*)", re.IGNORECASE)
_GLIESE_NAME = re.compile(
    r"^(?:GL|GJ|NN|WO)\s*0*(\d+(?:\.\d+)?)(?:\s*[A-Z]+)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class V70AComponentSemantics:
    native_code: str
    kind: str
    component_label: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def v70a_component_code(
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> str:
    if payload is not None:
        value = payload.get("Comp")
        if value is not None:
            return str(value).strip().upper()
    matched = _SOURCE_COMPONENT.search(source_id or "")
    return "" if matched is None else matched.group(1).strip().upper()


def decode_v70a_component(
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> V70AComponentSemantics:
    """Decode V/70A's A, B, C, ... component column."""
    code = v70a_component_code(payload, source_id)
    if not code:
        return V70AComponentSemantics("", "unspecified")
    if re.fullmatch(r"[A-Z]+", code):
        return V70AComponentSemantics(code, "named_component", code)
    return V70AComponentSemantics(code, "unknown")


def v70a_component_identifiers(
    identifiers: Iterable[str],
    payload: Mapping[str, object] | None = None,
    source_id: str | None = None,
) -> tuple[str, ...]:
    """Construct component-specific Gliese names for exact identity.

    ``Name`` is the system root in component rows, so ``GJ 1294`` plus
    ``Comp=B`` identifies ``GJ 1294 B``. Alternate columns are retained by
    ``lookup_identifiers`` as broad row-discovery aliases, but are not exact
    component identity: an unsuffixed HD designation can name the whole
    system, while an unsuffixed LHS designation can name one component.
    """
    values = tuple(str(value).strip() for value in identifiers if str(value).strip())
    component = decode_v70a_component(payload, source_id).component_label
    if not component:
        return values

    primary = ""
    if payload is not None and payload.get("Name") is not None:
        primary = str(payload["Name"]).strip()
    if not primary and values:
        primary = values[0]

    result: list[str] = []
    matched = _GLIESE_NAME.fullmatch(primary)
    if matched is not None:
        result.append(f"GJ {matched.group(1)} {component}")
    if primary:
        result.append(_append_component(primary, component))

    return tuple(dict.fromkeys(result))


def _append_component(value: str, component: str) -> str:
    normalized = " ".join(value.split())
    if (
        normalized.upper().endswith(f" {component.upper()}")
        or re.search(
            rf"(?<=\d){re.escape(component)}$",
            normalized,
            re.IGNORECASE,
        )
    ):
        return normalized
    return f"{normalized} {component}"
