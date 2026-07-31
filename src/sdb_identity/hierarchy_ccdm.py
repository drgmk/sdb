"""CCDM-specific fixed-width parsing and position policy."""

from __future__ import annotations

import math

from .hierarchy_records import ParsedHierarchyRecord, coords_from_hierarchy_id, float_value


def parse_fixed_width(line: str) -> ParsedHierarchyRecord | None:
    if not line.strip() or line.lstrip().startswith(("#", ";")):
        return None
    native = line[1:11].strip() if len(line) >= 11 else ""
    component = discoverer = None
    year = pa = separation = vmag = None
    raw_payload: dict[str, object] = {"line": line, "coordinate_source": "ccdm_id_only"}
    if native and ("+" in native or "-" in native):
        reference = line[11:12].strip()
        concerned = line[12:13].strip()
        component = component_label(reference, concerned)
        discoverer = " ".join(line[15:22].split()) or None
        raw_payload.update({
            "rComp": reference,
            "Comp": concerned,
            "component_label": component,
            "dRAs": float_value(line[23:30]),
            "dDEs": float_value(line[30:37]),
            "pmNote": line[66:67].strip(),
            "pmRA_masyr": float_value(line[67:72]),
            "pmDE_masyr": float_value(line[72:77]),
        })
        year = float_value(line[41:45])
        pa = float_value(line[46:49])
        separation = float_value(line[49:55])
        vmag = float_value(line[59:63])
    else:
        tokens = line.split()
        native = next(
            (token for token in tokens if token.upper().startswith("J") and len(token) >= 10),
            None,
        )
        if native is None and tokens and tokens[0].upper() == "CCDM" and len(tokens) > 1:
            native = tokens[1]
        if native is None:
            native = tokens[0] if tokens else None
    if native is None:
        return None
    native = native.replace("CCDM", "").strip()
    ra, dec = precise_position(native, raw_payload)
    if component is None:
        component = component_from_id(native)
    return ParsedHierarchyRecord(
        native_id=native,
        component=component,
        discoverer_id=discoverer,
        ra_deg=ra,
        dec_deg=dec,
        last_epoch=year,
        measure_epoch=year,
        separation_arcsec=separation,
        pa_deg=pa,
        magnitude_primary=vmag,
        raw_payload=raw_payload,
    )


def component_label(reference: str, component: str) -> str | None:
    if not reference and not component:
        return None
    return f"{reference}{component}".strip() or None


def precise_position(
    native: str, raw_payload: dict[str, object],
) -> tuple[float | None, float | None]:
    base_ra, base_dec = coords_from_hierarchy_id(native)
    if base_ra is None or base_dec is None:
        return base_ra, base_dec
    dra_seconds = raw_payload.get("dRAs")
    ddec_arcsec = raw_payload.get("dDEs")
    if dra_seconds is None and ddec_arcsec is None:
        raw_payload["coordinate_source"] = "ccdm_id_only"
        return base_ra, base_dec
    ra, dec = base_ra, base_dec
    if isinstance(dra_seconds, int | float) and math.isfinite(float(dra_seconds)):
        ra = (ra + float(dra_seconds) * 15.0 / 3600.0) % 360.0
    if isinstance(ddec_arcsec, int | float) and math.isfinite(float(ddec_arcsec)):
        dec += float(ddec_arcsec) / 3600.0
    raw_payload["coordinate_source"] = "ccdm_remainder"
    return ra, dec


def component_from_id(value: str) -> str | None:
    tail = ""
    for character in reversed(value.strip()):
        if character.isalpha():
            tail = character + tail
        else:
            break
    return tail or None
