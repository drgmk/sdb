"""Compact SED projection for the interactive review workspace."""

from __future__ import annotations

from collections.abc import Iterable


_COMPONENT_SYMBOLS = (
    "circle",
    "square",
    "diamond",
    "triangle-up",
    "triangle-down",
    "x",
    "cross",
    "star",
    "pentagon",
    "hexagon",
)


def build_review_sed(
    matrix: dict[str, object],
    proposals: Iterable[dict[str, object]],
    ambiguous_photometry: Iterable[dict[str, object]] = (),
) -> dict[str, object]:
    """Convert matrix photometry to SDF-backed SED points.

    The assignment matrix chooses one value for each displayed band (including
    one PSC/FSC value for an IRAS family). Current assignments determine the
    component and accepted state; proposals provide the component for grey
    preview points that have not yet been committed.
    """
    proposal_by_id = {
        int(proposal["measurement_id"]): proposal
        for proposal in proposals
        if proposal.get("measurement_id") is not None
    }
    columns = matrix.get("columns") or []
    component_by_sdbid = {
        str(column["sdbid"]): _column_component(column, len(columns))
        for column in columns
        if column.get("sdbid")
    }
    points: list[dict[str, object]] = []
    errors: list[str] = []
    for row in matrix.get("rows") or []:
        for band in row.get("bands") or []:
            try:
                measurement_id = _selected_measurement_id(band)
            except (TypeError, ValueError) as exc:
                errors.append(
                    f"{row.get('provider')} {band.get('band')}: {exc}"
                )
                continue
            proposal = proposal_by_id.get(measurement_id)
            if proposal is None:
                errors.append(
                    f"{row.get('provider')} {band.get('band')}: missing review proposal"
                )
                continue
            try:
                wavelength, flux, flux_error = _sdf_point(
                    str(band["band"]),
                    float(band["value"]),
                    float(band.get("error") or 0.0),
                    float(proposal.get("systematic_error") or 0.0),
                    str(band["unit"]),
                )
            # An unsupported SDF filter/unit should omit only that point, not
            # make the entire identity-review page unavailable.
            except Exception as exc:
                errors.append(
                    f"{row.get('provider')} {band.get('band')}: {exc}"
                )
                continue

            current = proposal.get("current_assignments") or []
            proposed = proposal.get("proposed_assignments") or []
            ambiguous = not current and not proposed
            component = (
                "ambiguous"
                if ambiguous
                else _assignment_component(
                    current or proposed,
                    component_by_sdbid,
                    catalog_component=proposal.get("catalog_component"),
                    single_component=next(
                        iter(component_by_sdbid.values()), None
                    )
                    if len(component_by_sdbid) == 1
                    else None,
                )
            )
            excluded = bool(proposal.get("excluded"))
            points.append(
                {
                    "measurement_id": measurement_id,
                    "provider": str(
                        proposal.get("provider") or row.get("provider")
                    ),
                    "source_id": str(
                        proposal.get("source_display_name")
                        or proposal.get("source_id")
                        or ""
                    ),
                    "band": str(band["band"]),
                    "wavelength_micron": wavelength,
                    "flux_jy": flux,
                    "error_jy": flux_error,
                    "component": component,
                    "accepted": bool(current) and not excluded,
                    "ambiguous": ambiguous,
                    "excluded": excluded,
                    "upper_limit": bool(proposal.get("upper_limit")),
                    "measurement_value": float(band["value"]),
                    "measurement_error": float(band.get("error") or 0.0),
                    "measurement_unit": str(band["unit"]),
                }
            )

    displayed_measurement_ids = {
        int(point["measurement_id"]) for point in points
    }
    for measurement in ambiguous_photometry:
        measurement_id = int(measurement["measurement_id"])
        if measurement_id in displayed_measurement_ids:
            continue
        try:
            wavelength, flux, flux_error = _sdf_point(
                str(measurement["band"]),
                float(measurement["value"]),
                float(measurement.get("error") or 0.0),
                float(measurement.get("systematic_error") or 0.0),
                str(measurement["unit"]),
            )
        except Exception as exc:
            errors.append(
                f"{measurement.get('provider')} {measurement.get('band')}: {exc}"
            )
            continue
        displayed_measurement_ids.add(measurement_id)
        excluded = bool(measurement.get("excluded"))
        points.append({
            "measurement_id": measurement_id,
            "provider": str(measurement.get("provider") or ""),
            "source_id": str(
                measurement.get("source_display_name")
                or measurement.get("source_id")
                or ""
            ),
            "band": str(measurement["band"]),
            "wavelength_micron": wavelength,
            "flux_jy": flux,
            "error_jy": flux_error,
            "component": "ambiguous",
            "accepted": False,
            "ambiguous": True,
            "excluded": excluded,
            "upper_limit": bool(measurement.get("upper_limit")),
            "measurement_value": float(measurement["value"]),
            "measurement_error": float(measurement.get("error") or 0.0),
            "measurement_unit": str(measurement["unit"]),
        })

    components = sorted({str(point["component"]) for point in points})
    symbols = {
        component: _COMPONENT_SYMBOLS[index % len(_COMPONENT_SYMBOLS)]
        for index, component in enumerate(components)
    }
    if "ambiguous" in symbols:
        symbols["ambiguous"] = "x"
    return {"points": points, "symbols": symbols, "errors": errors}


def _selected_measurement_id(band: dict[str, object]) -> int:
    selected = next(
        (
            entry.get("measurement_id")
            for entry in band.get("catalog_entries") or []
            if entry.get("selected")
        ),
        None,
    )
    ids = band.get("measurement_ids") or []
    value = selected if selected is not None else (ids[0] if ids else None)
    if value is None:
        raise ValueError("band has no measurement ID")
    return int(value)


def _column_component(column: dict[str, object], column_count: int) -> str:
    labels = [str(value) for value in column.get("component_labels") or []]
    if labels:
        return "/".join(labels)
    if column_count == 1:
        return "target"
    return str(column.get("label") or column.get("main_id") or column["sdbid"])


def _assignment_component(
    assignments: Iterable[dict[str, object]],
    component_by_sdbid: dict[str, str],
    *,
    catalog_component: object,
    single_component: str | None,
) -> str:
    rows = list(assignments)
    composite_labels = sorted(
        {
            component_by_sdbid.get(
                str(row.get("sdbid")), str(row.get("sdbid"))
            )
            for row in rows
            if row.get("sdbid") and row.get("role") == "composite_scope"
        }
    )
    if composite_labels:
        return "+".join(composite_labels)
    labels = sorted(
        {
            component_by_sdbid.get(
                str(row.get("sdbid")), str(row.get("sdbid"))
            )
            for row in rows
            if row.get("sdbid")
        }
    )
    if labels:
        return "+".join(labels)
    if isinstance(catalog_component, dict) and catalog_component.get("component_label"):
        return str(catalog_component["component_label"])
    return single_component or "unassigned"


def _sdf_point(
    band: str,
    value: float,
    error: float,
    systematic_error: float,
    unit: str,
) -> tuple[float, float, float]:
    """Return SDF mean wavelength, flux density, and uncertainty in Jy."""
    import contextlib
    import io

    import numpy as np
    import astropy.units as u

    # SDF's legacy configuration loader prints its discovered files at import
    # time. Review rendering is also used by JSON-producing CLI commands, so
    # keep that third-party diagnostic out of their stdout contract.
    with contextlib.redirect_stdout(io.StringIO()):
        from sdf.photometry import Photometry

    photometry = Photometry(
        filters=np.array([band]),
        measurement=np.array([value]),
        e_measurement=np.array([error]),
        s_measurement=np.array([systematic_error]),
        unit=np.array([u.Unit(unit)]),
        bibcode=np.array([""]),
        upperlim=np.array([False]),
        ignore=np.array([False]),
        note=np.array([""]),
    )
    photometry.fill_fnujy()
    return (
        float(photometry.mean_wavelength()[0]),
        float(photometry.fnujy[0]),
        float(photometry.e_fnujy[0]),
    )
