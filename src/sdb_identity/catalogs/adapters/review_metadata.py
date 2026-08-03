from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping


REVIEW_METADATA_KEY = "_sdb_review"


@dataclass(frozen=True)
class ReviewField:
    """A provider-native field which is useful during visual review."""

    key: str
    label: str
    columns: tuple[str, ...]
    unit: str | None = None
    neighbourhood: bool = False
    scale: float = 1.0


@dataclass(frozen=True)
class PositionUncertainty:
    """Provider-declared positional uncertainty columns and their units.

    Position angles, when present, are degrees east of north.
    """

    major_columns: tuple[str, ...]
    minor_columns: tuple[str, ...]
    scale_to_arcsec: float
    position_angle_columns: tuple[str, ...] = ()
    kind: str = "error_ellipse"


def add_review_metadata(
    payload: Mapping[str, object],
    *,
    fields: tuple[ReviewField, ...] = (),
    position_uncertainty: PositionUncertainty | tuple[PositionUncertainty, ...] | None = None,
) -> dict[str, object]:
    """Attach adapter-owned, provider-independent review metadata.

    Native catalog rows remain untouched.  Code outside adapters should use
    this normalized block instead of guessing meanings from column names.
    """

    result = dict(payload)
    normalized_fields = []
    neighbourhood: dict[str, object] = {}
    for definition in fields:
        found = _first_value(payload, definition.columns)
        if found is None:
            continue
        source_column, value = found
        if definition.scale != 1.0:
            try:
                value = float(value) * definition.scale
            except (TypeError, ValueError):
                continue
        normalized_fields.append({
            "key": definition.key,
            "label": definition.label,
            "value": value,
            "unit": definition.unit,
            "source_column": source_column,
        })
        if definition.neighbourhood:
            neighbourhood[definition.key] = value

    metadata: dict[str, object] = {}
    if normalized_fields:
        metadata["fields"] = normalized_fields
    if neighbourhood:
        metadata["neighbourhood_flags"] = neighbourhood

    uncertainty_definitions = (
        position_uncertainty
        if isinstance(position_uncertainty, tuple)
        else (() if position_uncertainty is None else (position_uncertainty,))
    )
    for definition in uncertainty_definitions:
        major = _first_float(payload, definition.major_columns, positive=True)
        minor = _first_float(payload, definition.minor_columns, positive=True)
        if major is not None or minor is not None:
            major_value = major or minor
            minor_value = minor or major
            assert major_value is not None and minor_value is not None
            uncertainty: dict[str, object] = {
                "major_arcsec": major_value[1] * definition.scale_to_arcsec,
                "minor_arcsec": minor_value[1] * definition.scale_to_arcsec,
                "kind": definition.kind,
                "source_columns": [major_value[0], minor_value[0]],
            }
            angle = _first_float(payload, definition.position_angle_columns)
            if angle is not None:
                uncertainty["position_angle_deg"] = angle[1]
                uncertainty["source_columns"].append(angle[0])
            metadata["position_uncertainty"] = uncertainty
            break

    if metadata:
        result[REVIEW_METADATA_KEY] = metadata
    else:
        result.pop(REVIEW_METADATA_KEY, None)
    return result


def review_metadata(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get(REVIEW_METADATA_KEY)
    return value if isinstance(value, dict) else {}


def normalize_review_payload(
    provider: str, payload: Mapping[str, object]
) -> dict[str, object]:
    """Apply the owning adapter's current review declarations to a raw row.

    This also makes review output deterministic for rows stored before review
    metadata was introduced or after an adapter declaration was corrected.
    """

    # Local imports avoid a module cycle: adapters use the declarations above
    # while review consumers call this dispatcher after adapter import.
    from .allwise import AllWiseAdapter
    from .gaia import GaiaDr3Adapter
    from .twomass import TwoMassAdapter
    from .tycho2 import Tycho2Adapter
    from .reference import IrasFscSnapshotAdapter, IrasPscSnapshotAdapter

    adapters = {
        AllWiseAdapter.name: AllWiseAdapter,
        GaiaDr3Adapter.name: GaiaDr3Adapter,
        TwoMassAdapter.name: TwoMassAdapter,
        Tycho2Adapter.name: Tycho2Adapter,
        "iras_psc": IrasPscSnapshotAdapter,
        "iras_fsc": IrasFscSnapshotAdapter,
    }
    adapter = adapters.get(provider)
    if adapter is None:
        return dict(payload)
    return add_review_metadata(
        payload,
        fields=adapter.review_fields,
        position_uncertainty=adapter.position_uncertainty,
    )


def _first_value(
    payload: Mapping[str, object], columns: tuple[str, ...]
) -> tuple[str, object] | None:
    names = {str(name).casefold(): str(name) for name in payload}
    for column in columns:
        source = names.get(column.casefold())
        if source is None:
            continue
        value = payload[source]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return source, value
    return None


def _first_float(
    payload: Mapping[str, object], columns: tuple[str, ...], *, positive: bool = False
) -> tuple[str, float] | None:
    found = _first_value(payload, columns)
    if found is None:
        return None
    source, raw = found
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or (positive and value <= 0):
        return None
    return source, value
