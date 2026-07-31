from __future__ import annotations

import html
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .catalog_policy import catalog_source_display_name
from .adapters.review_metadata import normalize_review_payload
from .assignment_review import build_measurement_assignment_review
from .catalog_provenance import vizier_entry_url
from .catalog_results import (
    effective_catalog_results,
    effective_catalog_selected_rows,
)

from .astrometry import propagate_to_epoch
from .hierarchy import (
    HierarchyService,
    _GRAPH_EDGE_STATUSES,
    _graph_edge_row,
    _latest_graph_overrides,
)
from .hierarchy_geometry import hierarchy_record_positions
from .hierarchy_wds import UNUSABLE_SEPARATION_ARCSEC
from .identity_results import effective_identity_candidate_ids
from .models import (
    AstrometricSolution,
    CatalogAttribute,
    CatalogDetectionProvenance,
    CatalogRun,
    HierarchyMatchCandidate,
    HierarchyRecord,
    MatchCandidate,
    NormalizedMeasurement,
    RawCatalogRow,
    SimbadMetadata,
    StructuralEdge,
    Submission,
    Target,
)
from .providers import Astrometry
from .targets import resolve_target
from .ubv_components import decode_ubv_component
from .tdsc_components import decode_tdsc_component
from .v70a_components import decode_v70a_component
from .vocabulary import PROVIDER_FAILURE_STATUSES


_HIERARCHY_VIZIER_LOCATORS = {
    "wds": ("B/wds/wds", "WDS"),
    "ccdm": ("I/274/ccdm", "CCDM"),
}


@dataclass(frozen=True)
class PhotometryBeam:
    provider: str
    band: str
    major_arcsec: float
    minor_arcsec: float | None = None
    kind: str | None = None
    reference: str | None = None
    ownership_scope: str = "component"
    blend_state: str = "clear"
    blend_reason: str | None = None
    value: float | None = None
    error: float | None = None
    unit: str | None = None
    upper_limit: bool = False


@dataclass(frozen=True)
class SkyPoint:
    kind: str
    provider: str
    status: str
    source_id: str
    ra_deg: float
    dec_deg: float
    separation_arcsec: float
    source_display_name: str = ""
    score: float | None = None
    accepted: bool = False
    run_id: int | None = None
    raw_row_id: int | None = None
    candidate_id: int | None = None
    detection_id: int | None = None
    target_id: int | None = None
    run_target_sdbid: str | None = None
    native_epoch: float | None = None
    native_ra_deg: float | None = None
    native_dec_deg: float | None = None
    display_epoch: float = 2000.0
    pm_ra_cosdec_masyr: float | None = None
    pm_dec_masyr: float | None = None
    pm_source: str | None = None
    photometry: tuple[str, ...] = ()
    photometry_beams: tuple[PhotometryBeam, ...] = ()
    attributes: tuple[str, ...] = ()
    linked_target_sdbids: tuple[str, ...] = ()
    cross_candidate_reason: str | None = None
    uncertainty_major_arcsec: float | None = None
    uncertainty_minor_arcsec: float | None = None
    catalog_component: str | None = None
    provenance: tuple[dict[str, object], ...] = ()
    note: str = ""


@dataclass(frozen=True)
class SkyArrow:
    kind: str
    provider: str
    source_id: str
    ra_deg: float
    dec_deg: float
    pm_ra_cosdec_masyr: float
    pm_dec_masyr: float
    years: float
    target_id: int | None = None
    note: str = ""


@dataclass(frozen=True)
class SkySegment:
    kind: str
    provider: str
    status: str
    source_id: str
    label: str
    start_ra_deg: float
    start_dec_deg: float
    end_ra_deg: float
    end_dec_deg: float
    candidate_id: int | None = None
    target_id: int | None = None
    native_id: str | None = None
    reference_label: str | None = None
    component_label: str | None = None
    relation_type: str = "component"
    structural_role: str = "non_structural"
    note: str = ""


@dataclass(frozen=True)
class ReviewSkyView:
    target_id: int
    sdbid: str
    center_ra_deg: float
    center_dec_deg: float
    radius_arcsec: float
    points: tuple[SkyPoint, ...]
    arrows: tuple[SkyArrow, ...] = ()
    segments: tuple[SkySegment, ...] = ()
    system_context: dict[str, object] | None = None


def build_review_sky_view(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    radius_arcsec: float | None = None,
) -> ReviewSkyView:
    if radius_arcsec is not None:
        radius_arcsec = float(radius_arcsec)
        if (
            not math.isfinite(radius_arcsec)
            or not 1.0 <= radius_arcsec <= 600.0
        ):
            raise ValueError("review radius must be between 1 and 600 arcsec")
    system_context = None
    with session_factory() as session:
        target = resolve_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        center = _target_center(session, target)
        solution = _target_solution(session, target)
        motion_solution = _target_motion_solution(session, target, solution)
        points = [
            SkyPoint(
                kind="target",
                provider="sdb",
                status="target",
                source_id=target.sdbid,
                ra_deg=center[0],
                dec_deg=center[1],
                separation_arcsec=0.0,
                accepted=True,
                target_id=target.id,
                pm_ra_cosdec_masyr=(
                    None
                    if motion_solution is None
                    else motion_solution.pm_ra_cosdec_masyr
                ),
                pm_dec_masyr=(
                    None if motion_solution is None else motion_solution.pm_dec_masyr
                ),
                pm_source=(
                    None if motion_solution is None else motion_solution.source
                ),
                note="canonical target position",
            )
        ]
        points.extend(
            _identity_points(
                session, target, center, motion_solution=motion_solution,
            )
        )
        points.extend(
            _catalog_points(
                session, target, center, motion_solution=motion_solution,
            )
        )
        points.extend(_simbad_metadata_points(session, target, center))
        hierarchy_points, hierarchy_segments = _hierarchy_points(session, target, center)
        points.extend(hierarchy_points)
        points = _deduplicate_points(points)
        arrows = []
        if motion_solution is not None:
            arrows.extend(_proper_motion_arrows(target, motion_solution))
        segments = list(hierarchy_segments)
        hierarchy_service = HierarchyService(session_factory)
        system_context = hierarchy_service.system_context(
            target.sdbid,
            radius_arcsec=radius_arcsec,
        )

        if radius_arcsec is None:
            farthest = max((point.separation_arcsec for point in points), default=1.0)
            farthest = max(
                farthest,
                max((_segment_farthest_offset(center, segment) for segment in segments), default=0.0),
            )
            explicit_member_sdbids = set(
                system_context.get("system_memberships_by_target") or {}
            )
            explicit_member_farthest = max(
                (
                    float(row["separation_arcsec"])
                    for row in system_context.get("nearby_sdb_targets") or []
                    if row.get("sdbid") in explicit_member_sdbids
                ),
                default=0.0,
            )
            farthest = max(farthest, explicit_member_farthest)
            radius_arcsec = min(600.0, max(60.0, math.ceil(farthest * 1.25)))
            if radius_arcsec != system_context["radius_arcsec"]:
                system_context = hierarchy_service.system_context(
                    target.sdbid,
                    radius_arcsec=radius_arcsec,
                )

        points = _annotate_catalog_target_candidates(
            session,
            target,
            center,
            points,
            system_context=system_context,
        )
        points = _deduplicate_points(points)
        assignment_review = build_measurement_assignment_review(
            session_factory,
            target.sdbid,
            system_context=system_context,
        )
        system_context["measurement_assignment_proposals"] = (
            assignment_review.proposals
        )
        system_context["measurement_assignment_matrix"] = assignment_review.matrix

        nearby_points, nearby_arrows = _nearby_target_points(
            session,
            target,
            center,
            radius_arcsec,
        )
        points.extend(nearby_points)
        points = _deduplicate_points(points)
        arrows.extend(nearby_arrows)
        points = _annotate_identity_cross_candidates(points, system_context)

    return ReviewSkyView(
        target_id=target.id,
        sdbid=target.sdbid,
        center_ra_deg=center[0],
        center_dec_deg=center[1],
        radius_arcsec=radius_arcsec,
        points=tuple(points),
        arrows=tuple(arrows),
        segments=tuple(segments),
        system_context=system_context,
    )


def write_review_sky_html(view: ReviewSkyView, output: str | Path) -> Path:
    path = Path(output)
    path.write_text(render_review_sky_html(view), encoding="utf-8")
    return path


def render_review_sky_html(
    view: ReviewSkyView, *, embedded: bool = False,
) -> str:
    import plotly.graph_objects as go
    import plotly.io as pio

    payload = _view_payload(view)
    title = f"SDB review sky view: {view.sdbid}"
    status_colors = {
        "target": "#111827",
        "nearby": "#0f766e",
        "match": "#f59e0b",
        "accepted": "#16a34a",
        "ambiguous": "#f59e0b",
        "no_match": "#2563eb",
        "rejected": "#2563eb",
        "transient_failure": "#dc2626",
        "permanent_failure": "#991b1b",
        "candidate": "#2563eb",
        "review_neighbour": "#94a3b8",
    }
    provider_symbols = ["circle", "square", "triangle-up", "diamond", "x", "cross", "star", "hexagon"]
    providers = sorted({point["provider"] for point in payload["points"]})
    provider_symbol = {
        provider: provider_symbols[index % len(provider_symbols)]
        for index, provider in enumerate(providers)
    }

    def color_for(point: dict[str, object]) -> str:
        if point["kind"] == "identity" and not point["accepted"]:
            return status_colors["candidate"]
        return status_colors.get(str(point["status"]), "#64748b")

    beam_colors = [
        "#7c3aed", "#0891b2", "#db2777", "#ca8a04",
        "#059669", "#dc2626", "#4f46e5", "#ea580c",
    ]

    def beam_color(index: int) -> str:
        return beam_colors[index % len(beam_colors)]

    def ellipse_xy(
        x_center: float,
        y_center: float,
        major_arcsec: float,
        minor_arcsec: float | None,
        *,
        samples: int = 96,
    ) -> tuple[list[float], list[float]]:
        x_radius = major_arcsec / 2.0
        y_radius = (minor_arcsec if minor_arcsec is not None else major_arcsec) / 2.0
        angles = [2.0 * math.pi * index / samples for index in range(samples + 1)]
        return (
            [x_center + x_radius * math.cos(angle) for angle in angles],
            [y_center + y_radius * math.sin(angle) for angle in angles],
        )

    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for point in payload["points"]:
        grouped.setdefault((str(point["provider"]), str(point["status"])), []).append(point)
    hierarchy_segment_by_candidate = {
        segment["candidate_id"]: segment
        for segment in payload["segments"]
        if segment.get("candidate_id") is not None
    }

    def point_display_id(point: dict[str, object]) -> object:
        return point.get("source_display_name") or point["source_id"]

    def point_hover_text(point: dict[str, object]) -> str:
        if (
            point.get("kind") == "hierarchy"
            and point.get("provider") in {"wds", "ccdm"}
        ):
            segment = hierarchy_segment_by_candidate.get(
                point.get("candidate_id")
            )
            system = (
                point["source_id"]
                if segment is None
                else segment.get("native_id") or point["source_id"]
            )
            component = (
                None
                if segment is None
                else segment.get("component_label") or segment.get("label")
            )
            component_line = (
                "" if not component else f"<br>component {component}"
            )
            return (
                f"{point['provider']} {system}"
                f"{component_line}<br>{point['status']}<br>"
                f"separation {_compact_display_value(float(point['separation_arcsec']))}\""
            )
        return f"{point['provider']} {point_display_id(point)}"

    figure = go.Figure()
    for (provider, status), points in grouped.items():
        first = points[0]
        figure.add_trace(go.Scatter(
            x=[point["x_arcsec"] for point in points],
            y=[point["y_arcsec"] for point in points],
            mode="markers",
            name=f"{provider} / {status}",
            text=[point_hover_text(point) for point in points],
            customdata=[point["index"] for point in points],
            meta={"review_kind": "points"},
            marker={
                "symbol": [
                    "circle-open" if point["status"] == "review_neighbour"
                    else provider_symbol.get(provider, "circle")
                    for point in points
                ],
                "color": [color_for(point) for point in points],
                "size": [
                    9 if point["status"] == "review_neighbour"
                    else 16 if point["kind"] == "target"
                    else 13 if point["accepted"]
                    else 11
                    for point in points
                ],
                "opacity": [0.55 if point["status"] in {"no_match", "review_neighbour"} else 0.95 for point in points],
                "line": {
                    "color": [
                        "#7c3aed" if point.get("linked_target_sdbids") else (
                            "#111827" if point["accepted"] else color_for(point)
                        )
                        for point in points
                    ],
                    "width": [
                        3 if point.get("linked_target_sdbids") else (
                            2 if point["accepted"] else 1
                        )
                        for point in points
                    ],
                },
            },
            hovertemplate=(
                "%{text}<br>"
                "x=%{x:.2f}\" east<br>"
                "y=%{y:.2f}\" north"
                f"<extra>{provider} / {status}</extra>"
            ),
        ))

    for point in payload["points"]:
        for beam_index, beam in enumerate(point.get("photometry_beams") or []):
            major = beam.get("major_arcsec")
            if major is None:
                continue
            minor = beam.get("minor_arcsec")
            x_values, y_values = ellipse_xy(
                float(point["x_arcsec"]),
                float(point["y_arcsec"]),
                float(major),
                None if minor is None else float(minor),
            )
            kind = beam.get("kind") or "resolution"
            reference = beam.get("reference") or ""
            width_label = (
                f"{_compact_display_value(float(major))}\""
                if minor is None or math.isclose(float(major), float(minor))
                else (
                    f"{_compact_display_value(float(major))}\" × "
                    f"{_compact_display_value(float(minor))}\""
                )
            )
            value_label = "" if beam.get("value") is None else (
                f"<br>value: {'<' if beam.get('upper_limit') else ''}"
                f"{_compact_display_value(float(beam['value']))} {beam.get('unit') or ''}"
            )
            label = (
                f"{beam.get('provider')} {beam.get('band')}<br>"
                f"{kind}: {width_label} full width<br>"
                f"scope: {beam.get('ownership_scope')}; blend: {beam.get('blend_state')}"
                f"{value_label}<br>"
                f"{reference}"
            )
            figure.add_trace(go.Scatter(
                x=x_values,
                y=y_values,
                mode="lines",
                name=f"beam {beam.get('provider')} {beam.get('band')}",
                text=label,
                meta={
                    "review_kind": "beam",
                    "point_index": point["index"],
                    "band": beam.get("band"),
                    "provider": beam.get("provider"),
                },
                line={
                    "color": beam_color(beam_index),
                    "width": 2,
                    "dash": "dot",
                },
                fill="toself",
                fillcolor="rgba(124, 58, 237, 0.035)",
                opacity=0.75,
                visible=False,
                showlegend=False,
                hovertemplate="%{text}<br>x=%{x:.2f}\" east<br>y=%{y:.2f}\" north<extra>photometry beam</extra>",
            ))

    if payload["paths"]:
        for path in payload["paths"]:
            label = (
                f"{path['provider']} {path['source_id']}<br>"
                f"epoch {_compact_display_value(path['native_epoch'])} to "
                f"{_compact_display_value(path['display_epoch'])}<br>"
                f"PM source: {path['pm_source'] or 'unknown'}"
            )
            figure.add_trace(go.Scatter(
                x=[path["x_native_arcsec"], path["x_display_arcsec"]],
                y=[path["y_native_arcsec"], path["y_display_arcsec"]],
                mode="lines",
                name="catalog epoch to 2000",
                text=[label, label],
                meta={"review_kind": "line", "point_index": path["index"]},
                line={"color": "#f97316", "width": 2, "dash": "dot"},
                opacity=0.95,
                showlegend=path is payload["paths"][0],
                hovertemplate="%{text}<br>x=%{x:.2f}\" east<br>y=%{y:.2f}\" north<extra>epoch path</extra>",
            ))

    if payload["segments"]:
        for index, segment in enumerate(payload["segments"]):
            relation_type = str(segment.get("relation_type") or "component")
            line_width = 1 if relation_type == "cross_link" else 2
            line_dash = "dot" if relation_type == "cross_link" else ("solid" if relation_type == "internal" else "dash")
            line_opacity = 0.32 if relation_type == "cross_link" else 0.85
            point = next(
                (
                    value for value in payload["points"]
                    if value["index"] == segment.get("point_index")
                ),
                None,
            )
            separation = (
                ""
                if point is None
                else (
                    f"<br>separation "
                    f"{_compact_display_value(float(point['separation_arcsec']))}\""
                )
            )
            label = (
                f"{segment['provider']} "
                f"{segment.get('native_id') or segment['source_id']}<br>"
                f"component {segment.get('component_label') or segment['label']}<br>"
                f"{segment['status']}{separation}"
            )
            figure.add_trace(go.Scatter(
                x=[segment["x_start_arcsec"], segment["x_end_arcsec"]],
                y=[segment["y_start_arcsec"], segment["y_end_arcsec"]],
                mode="lines+markers+text",
                name=f"hierarchy {relation_type} / {segment['provider']}",
                text=[label, label],
                texttemplate=[
                    "",
                    f"{segment.get('component_label') or segment['label']} {relation_type}".strip(),
                ],
                textposition="top right",
                meta={"review_kind": "line", "point_index": segment.get("point_index"), "relation_type": relation_type},
                line={"color": color_for(segment), "width": line_width, "dash": line_dash},
                marker={"color": color_for(segment), "size": [5, 6]},
                opacity=line_opacity,
                showlegend=index == 0,
                hovertemplate="%{text}<br>x=%{x:.2f}\" east<br>y=%{y:.2f}\" north<extra>component link</extra>",
            ))

    if payload["arrows"]:
        for index, arrow in enumerate(payload["arrows"]):
            label = (
                f"{arrow['provider']} {arrow['source_id']}<br>"
                f"{_compact_display_value(arrow['years'])} yr PM vector<br>"
                f"pmRA*={_compact_display_value(arrow['pm_ra_cosdec_masyr'])} mas/yr, "
                f"pmDec={_compact_display_value(arrow['pm_dec_masyr'])} mas/yr"
            )
            figure.add_trace(go.Scatter(
                x=[arrow["x_arcsec"], arrow["x_end_arcsec"]],
                y=[arrow["y_arcsec"], arrow["y_end_arcsec"]],
                mode="lines+markers",
                name=f"proper motion / {_compact_display_value(arrow['years'])} yr",
                text=[label, label],
                meta={"review_kind": "line", "point_index": arrow.get("point_index")},
                line={"color": "#ef4444", "width": 3},
                marker={"color": "#ef4444", "size": 6},
                opacity=0.95,
                showlegend=index == 0,
                hovertemplate="%{text}<br>x=%{x:.2f}\" east<br>y=%{y:.2f}\" north<extra>PM</extra>",
            ))

    shapes = [
        {
            "type": "circle",
            "xref": "x",
            "yref": "y",
            "x0": -2.0,
            "x1": 2.0,
            "y0": -2.0,
            "y1": 2.0,
            "line": {"color": "rgba(17, 24, 39, 0.35)", "width": 1, "dash": "dot"},
            "fillcolor": "rgba(17, 24, 39, 0.03)",
        }
    ]
    for point in payload["points"]:
        major = point.get("uncertainty_major_arcsec")
        minor = point.get("uncertainty_minor_arcsec") or major
        if major is None or minor is None:
            continue
        shapes.append({
            "type": "circle",
            "xref": "x",
            "yref": "y",
            "x0": point["x_arcsec"] - major,
            "x1": point["x_arcsec"] + major,
            "y0": point["y_arcsec"] - minor,
            "y1": point["y_arcsec"] + minor,
            "line": {"color": "rgba(37, 99, 235, 0.35)", "width": 1, "dash": "dash"},
            "fillcolor": "rgba(37, 99, 235, 0.04)",
        })

    label_annotations = [
        {
            "x": point["x_arcsec"],
            "y": point["y_arcsec"],
            "text": f"{point['provider']} {point_display_id(point)}",
            "showarrow": False,
            "xanchor": "left",
            "yanchor": "bottom",
            "xshift": 8,
            "yshift": 6,
            "font": {"size": 11, "color": "#111827"},
            "bgcolor": "rgba(255,255,255,0.72)",
            "borderpad": 1,
        }
        for point in payload["points"]
    ]

    figure.update_layout(
        title=None,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 60, "r": 20, "t": 28, "b": 60},
        hovermode="closest",
        annotations=label_annotations,
        shapes=shapes,
        xaxis={
            "title": "arcsec east (east plotted left)",
            "range": [view.radius_arcsec, -view.radius_arcsec],
            "zeroline": True,
            "zerolinecolor": "#64748b",
            "gridcolor": "#cbd5e1",
            "scaleanchor": "y",
            "scaleratio": 1,
        },
        yaxis={
            "title": "arcsec north",
            "range": [-view.radius_arcsec, view.radius_arcsec],
            "zeroline": True,
            "zerolinecolor": "#64748b",
            "gridcolor": "#cbd5e1",
        },
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        height=690,
    )

    plot_html = pio.to_html(
        figure,
        include_plotlyjs=True,
        full_html=False,
        div_id="sky",
        config={"responsive": False, "scrollZoom": True, "displaylogo": False},
    )
    payload_json = json.dumps(payload, sort_keys=True)
    label_json = json.dumps(label_annotations, sort_keys=True)
    escaped_title = html.escape(title)
    body_class = "embedded" if embedded else "standalone"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f8fafc;
      --fg: #111827;
      --muted: #64748b;
      --panel: #ffffff;
      --grid: #cbd5e1;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{ --bg: #0f172a; --fg: #e5e7eb; --muted: #94a3b8; --panel: #111827; --grid: #334155; }}
    }}
    body {{ margin: 0; font: 14px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); }}
    body.embedded header {{ display: none; }}
    body.embedded main {{ padding-top: 8px; }}
    header {{ max-width: none; margin: 0 auto; padding: 16px; }}
    main {{ box-sizing: border-box; width: 100%; max-width: 2100px; margin: 0 auto; padding: 16px; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 12px 0; }}
    button {{ border: 1px solid var(--grid); border-radius: 6px; padding: 6px 10px; background: var(--panel); color: var(--fg); cursor: pointer; }}
    .layout {{ display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 6fr) minmax(0, 3fr) minmax(0, 3.5fr); gap: 16px; align-items: start; }}
    @media (max-width: 1700px) {{ .layout {{ grid-template-columns: minmax(0, 2fr) minmax(0, 6fr) minmax(0, 3fr); }} .photometry-panel {{ grid-column: 1 / -1; }} }}
    @media (max-width: 1350px) {{ .layout {{ grid-template-columns: minmax(0, 2fr) minmax(0, 6fr); }} .items-panel, .photometry-panel {{ grid-column: 1 / -1; }} }}
    @media (max-width: 980px) {{ .layout {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 900px) {{ .layout {{ grid-template-columns: 1fr; }} }}
    .sky-frame {{ box-sizing: border-box; width: 100%; aspect-ratio: 1 / 1; min-height: 0; background: var(--panel); border: 1px solid var(--grid); border-radius: 8px; }}
    #sky {{ box-sizing: border-box; width: 100% !important; height: 100% !important; background: transparent; }}
    .plot-column {{ min-width: 0; }}
    .plot-controls {{ margin: 0 0 8px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--grid); border-radius: 8px; padding: 12px; }}
    .point-row {{ display: grid; grid-template-columns: 18px 1fr; gap: 8px; align-items: start; margin: 7px 0; cursor: pointer; }}
    .point-primary {{ display: block; white-space: nowrap; }}
    .point-secondary {{ display: block; }}
    .matrix-heading {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .matrix-heading h3 {{ margin: 0; }}
    .review-drawer-toggle[aria-pressed="true"] {{ background: #dbeafe; border-color: #60a5fa; color: #1e40af; }}
    .matrix-wrap {{ overflow-x: auto; margin: 8px 0 12px; }}
    .assignment-matrix {{ border-collapse: collapse; font-size: 0.76rem; width: 100%; }}
    .assignment-matrix th, .assignment-matrix td {{ border: 1px solid #d7dee8; padding: 4px 5px; text-align: center; vertical-align: middle; }}
    .assignment-matrix th:first-child, .assignment-matrix td:first-child {{ text-align: left; min-width: 92px; }}
    .matrix-source {{ display: block; max-width: none; white-space: normal; overflow-wrap: anywhere; }}
    .matrix-cell.agrees {{ background: #dcfce7; color: #166534; }}
    .matrix-cell.proposed {{ background: #ffedd5; color: #9a3412; }}
    .matrix-cell.current_only {{ background: #dbeafe; color: #1e40af; }}
    .matrix-cell.differs {{ background: #fee2e2; color: #991b1b; }}
    .matrix-cell.candidate {{ background: #f1f5f9; color: #64748b; }}
    .matrix-info {{ cursor: help; font-weight: 700; color: #475569; }}
    .matrix-warning {{ color: #92400e; font-weight: 700; }}
    .details-panel {{ margin-top: 16px; }}
    .details-columns {{ display: grid; grid-template-columns: minmax(210px, 0.8fr) minmax(300px, 1.35fr); gap: 12px; }}
    .detail-list {{ margin: 0; min-width: 0; }}
    .detail-row {{ display: grid; grid-template-columns: minmax(88px, max-content) minmax(0, 1fr); column-gap: 8px; padding: 3px 0; border-bottom: 1px solid color-mix(in srgb, var(--grid) 55%, transparent); }}
    .detail-row dt, .detail-row dd {{ margin: 0; overflow-wrap: anywhere; }}
    @media (max-width: 700px) {{ .details-columns {{ grid-template-columns: 1fr; }} }}
    .point-row.dimmed {{ opacity: 0.35; }}
    .point-row.selected {{ outline: 1px solid var(--grid); border-radius: 5px; background: color-mix(in srgb, var(--panel) 78%, var(--grid)); }}
    .point-list-controls {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }}
    .tree-group {{ margin: 0 0 12px; padding-bottom: 10px; border-bottom: 1px solid var(--grid); }}
    .tree-title {{ font-weight: 700; margin-bottom: 5px; }}
    .tree-link {{ margin: 5px 0 5px 12px; padding-left: 12px; border-left: 2px solid var(--grid); cursor: pointer; }}
    .tree-link.selected {{ outline: 1px solid var(--grid); border-radius: 5px; background: color-mix(in srgb, var(--panel) 78%, var(--grid)); }}
    .tree-link.dimmed {{ opacity: 0.35; }}
    .system-section {{ margin-top: 0; padding-top: 0; border-top: 0; }}
    .system-row {{ margin: 8px 0; padding-left: 10px; border-left: 2px solid var(--grid); }}
    .system-name {{ font-weight: 700; }}
    .system-properties {{ margin-top: 3px; }}
    .system-radius-control {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    .system-radius-control input {{ box-sizing: border-box; width: 74px; border: 1px solid var(--grid); border-radius: 5px; padding: 5px 6px; background: var(--panel); color: var(--fg); }}
    .system-radius-control button {{ padding: 5px 9px; }}
    .system-list {{ margin: 6px 0 10px 18px; padding: 0; }}
    .system-list li {{ margin: 4px 0; }}
    .relative-review {{ width: 100%; margin: 4px 0 10px; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 50%; border: 1px solid #111827; margin-top: 3px; }}
    .muted {{ color: var(--muted); }}
    .accepted {{ font-weight: 700; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    dt {{ font-weight: 700; margin-top: 0.4rem; }}
    dd {{ margin-left: 0; color: var(--muted); }}
  </style>
</head>
<body class="{body_class}">
  <header>
    <h1>{escaped_title}</h1>
    <div class="muted">Center RA={_compact_display_value(view.center_ra_deg)} deg, Dec={_compact_display_value(view.center_dec_deg)} deg; view radius={_compact_display_value(view.radius_arcsec)}".</div>
  </header>
  <main class="layout">
    <aside class="panel context-panel">
      <h2>Current target</h2>
      <div id="current-target" class="muted">No current target summary.</div>
      <h2>System context</h2>
      <div id="system-context" class="muted">No system context for this target.</div>
      <h2>Catalog hierarchy &amp; components</h2>
      <div id="hierarchy-tree" class="muted">No hierarchy links for this target.</div>
    </aside>
    <section class="plot-column">
      <div class="controls plot-controls">
        <button id="toggle-annotations" type="button">Hide target labels</button>
        <button id="toggle-beams" type="button">Show photometry beams</button>
        <span class="muted">East is left; north is up.</span>
      </div>
      <div class="sky-frame">{plot_html}</div>
      <aside class="panel details-panel">
        <h2>Selected point</h2>
        <div id="details" class="muted">Click a point in the sky view or plotted item list.</div>
      </aside>
    </section>
    <aside class="panel items-panel">
      <h2>Plotted items</h2>
      <div class="point-list-controls"><button id="toggle-point-list" type="button">Show all plotted items</button><span id="point-list-summary" class="muted"></span></div>
      <div id="points"></div>
    </aside>
    <aside class="panel photometry-panel">
      <h2>Photometry</h2>
      <div id="photometry-context" class="muted">No current measurements.</div>
    </aside>
  </main>
  <script>
    const view = {payload_json};
    const labelAnnotations = {label_json};
    const statusColors = {{target: "#111827", nearby: "#0f766e", match: "#f59e0b", accepted: "#16a34a", ambiguous: "#f59e0b", no_match: "#2563eb", rejected: "#2563eb", transient_failure: "#dc2626", permanent_failure: "#991b1b", candidate: "#2563eb", review_neighbour: "#94a3b8"}};
    let annotationsVisible = true;
    let beamsVisible = false;
    let selectedPointIndex = null;
    let clickedPlotItem = false;
    let pointListExpanded = false;
    let reviewDrawerVisible = false;
    function escapeHtml(value) {{ return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"); }}
    function sourceLink(label, provenance) {{
      const text = escapeHtml(String(label));
      const item = (provenance || []).find(value =>
        String(value.access_url || "").startsWith("https://")
      );
      if (!item) return text;
      return `<a href="${{escapeHtml(String(item.access_url))}}" target="_blank" rel="noopener">${{text}}</a>`;
    }}
    function pointDisplayId(point) {{ return point.source_display_name || point.source_id; }}
    function pointColor(point) {{ if (point.kind === "identity" && !point.accepted) return statusColors.candidate; return statusColors[point.status] || "#64748b"; }}
    function defaultPointOpacity(point) {{ return point.status === "no_match" || point.status === "review_neighbour" ? 0.55 : 0.95; }}
    function defaultLineOpacity(trace) {{ return trace.meta && trace.meta.relation_type === "cross_link" ? 0.32 : 0.95; }}
    function listValue(values) {{ return values && values.length ? values.join("; ") : ""; }}
    function displayNumber(value) {{
      if (value == null || value === "") return "";
      const number = Number(value);
      if (!Number.isFinite(number)) return String(value);
      const absolute = Math.abs(number);
      if (absolute === 0 || absolute >= 0.01) return number.toFixed(2);
      const decimals = Math.min(10, Math.max(3, Math.ceil(-Math.log10(absolute)) + 1));
      return number.toFixed(decimals);
    }}
    function beamValue(beams) {{
      if (!beams || !beams.length) return "";
      return beams.map(beam => {{
        const minor = beam.minor_arcsec ?? beam.major_arcsec;
        const width = Math.abs(minor - beam.major_arcsec) < 1e-6 ? `${{displayNumber(beam.major_arcsec)}}"` : `${{displayNumber(beam.major_arcsec)}}" × ${{displayNumber(minor)}}"`;
        const kind = beam.kind || "resolution";
        const value = beam.value == null ? "" : `; ${{beam.upper_limit ? "<" : ""}}${{displayNumber(beam.value)}} ${{beam.unit || ""}}`;
        return `${{beam.provider}} ${{beam.band}}: ${{kind}} ${{width}} full width; ${{beam.ownership_scope}}; ${{beam.blend_state}}${{value}}`;
      }}).join("; ");
    }}
    function linkedTargetValue(point) {{
      return point.linked_target_sdbids && point.linked_target_sdbids.length
        ? ` → ${{point.linked_target_sdbids.map(value => targetMainId(value)).join(", ")}}`
        : "";
    }}
    function catalogRunTargetValue(point) {{
      if (!point.run_target_sdbid || point.run_target_sdbid === view.sdbid) return "";
      return ` · catalog query for ${{targetMainId(point.run_target_sdbid)}}`;
    }}
    function targetReviewLink(sdbid, label) {{
      return `<a href="/target/${{encodeURIComponent(sdbid)}}" target="_top">${{escapeHtml(String(label || sdbid))}}</a>`;
    }}
    function targetMainId(sdbid, fallback) {{
      const context = view.system_context || {{}};
      const displayIds = context.simbad_main_id_by_target || {{}};
      if (displayIds[sdbid]) return displayIds[sdbid];
      const metadata = (context.simbad_metadata_by_target || {{}})[sdbid] || {{}};
      if (metadata.main_id) return metadata.main_id;
      const relative = (context.simbad_relative_preview || []).find(
        row => row.matched_sdbid === sdbid && row.action !== "context_only"
      );
      return relative && relative.main_id ? relative.main_id : (fallback || sdbid);
    }}
    function componentKey(provider, nativeId, component) {{
      return `${{provider}}|${{nativeId}}|${{component}}`;
    }}
    function componentStatusMap() {{
      const context = view.system_context || {{}};
      const result = {{}};
      for (const row of context.component_positions || []) {{
        result[componentKey(row.provider, row.native_id, row.component)] = row;
      }}
      return result;
    }}
    function componentTargetDetail(row) {{
      if (!row) return "";
      const bits = [];
      const role = row.component_target_role || "unknown";
      if (!row.linked_sdbid && role === "known_unimported_component") bits.push("no SDB target");
      else if (!row.linked_sdbid && role === "conflicted_component_assignment") bits.push("conflict: unassigned");
      else if (!row.linked_sdbid && role !== "current_target" && role !== "sibling_target") bits.push(role);
      if (row.component_match_basis && row.component_match_basis !== "none") bits.push(row.component_match_basis);
      if (row.component_match_separation_arcsec != null) bits.push(`${{displayNumber(row.component_match_separation_arcsec)}}"`);
      if (row.component_match_conflict) bits.push(row.component_match_conflict);
      return bits.filter(Boolean).join(" · ");
    }}
    function componentTargetHtml(row) {{
      if (!row) return "";
      const detail = componentTargetDetail(row);
      if (!row.linked_sdbid) return escapeHtml(detail);
      const suffix = detail ? ` <span class="muted">· ${{escapeHtml(detail)}}</span>` : "";
      return `${{targetReviewLink(row.linked_sdbid, targetMainId(row.linked_sdbid))}}${{suffix}}`;
    }}
    function renderSystemContext() {{
      const currentTargetElement = document.getElementById("current-target");
      const element = document.getElementById("system-context");
      const photometryElement = document.getElementById("photometry-context");
      const context = view.system_context;
      if (!context) return;
      currentTargetElement.classList.remove("muted");
      element.classList.remove("muted");
      photometryElement.classList.remove("muted");
      const semantics = context.simbad_semantic_by_target || {{}};
      const simbadMetadata = context.simbad_metadata_by_target || {{}};
      const targets = context.nearby_sdb_targets || [];
      const cross = context.identity_cross_candidates || [];
      const catalogCross = context.catalog_target_candidates || [];
      const matrix = context.measurement_assignment_matrix || {{columns: [], rows: [], summary: {{}}}};
      const relatives = context.simbad_relative_preview || [];
      const targetBySdbid = Object.fromEntries(targets.map(target => [target.sdbid, target]));
      const currentTarget = targets.find(target => target.is_requested_target) || null;
      const relativeTargetIds = new Set(
        relatives
          .filter(row => row.action !== "context_only")
          .map(row => row.matched_sdbid)
          .filter(Boolean)
      );
      const nearbyTargetCount = targets.filter(target => !target.is_requested_target).length;
      const otherTargets = targets.filter(
        target => !target.is_requested_target && !relativeTargetIds.has(target.sdbid)
      );
      const relevanceLabel = value => ({{
        stellar_or_substellar_component: "stellar",
        planetary_or_disk: "planet",
        contextual_group: "contextual group",
      }})[value] || String(value || "unknown").replaceAll("_", " ");
      const relativeActionLabel = value => ({{
        import: "ready to import",
        reconcile: "needs reconciliation",
        complete: "already reconciled",
        context_only: "context only",
        review_required: "review required",
      }})[value] || value;
      const targetFacts = (target, relative) => {{
        const simbad = target ? (simbadMetadata[target.sdbid] || {{}}) : {{}};
        const semantic = target ? (semantics[target.sdbid] || {{}}) : {{}};
        const pm = target && target.canonical_astrometry && target.canonical_astrometry.proper_motion_available ? `; PM ${{displayNumber(target.canonical_astrometry.pm_ra_cosdec_masyr)}}, ${{displayNumber(target.canonical_astrometry.pm_dec_masyr)}} mas/yr` : "";
        const distance = simbad.distance_pc == null ? "" : (
          simbad.distance_error_pc == null
            ? `${{displayNumber(simbad.distance_pc)}} pc`
            : `${{displayNumber(simbad.distance_pc)}} ± ${{displayNumber(simbad.distance_error_pc)}} pc`
        );
        const properties = [
          distance ? `d = ${{distance}}` : "",
          simbad.spectral_type || (relative && relative.spectral_type) ? `SpT ${{simbad.spectral_type || relative.spectral_type}}` : "",
          simbad.primary_object_type || (relative && relative.object_type) ? `type ${{simbad.primary_object_type || relative.object_type}}` : "",
        ].filter(Boolean).join(" · ");
        return {{properties, semantic: String(semantic.kind || "").replaceAll("_", " "), pm}};
      }};
      const targetRow = (target, relative, footer, linkName = true) => {{
        const facts = targetFacts(target, relative);
        const fallback = relative && relative.main_id ? relative.main_id : target.sdbid;
        const name = targetMainId(target.sdbid, fallback);
        const displayedName = linkName
          ? targetReviewLink(target.sdbid, name)
          : escapeHtml(String(name));
        const separation = !target.is_requested_target && Number(target.separation_arcsec) > 0.005
          ? `<div class="muted">${{displayNumber(target.separation_arcsec)}}" from target</div>`
          : "";
        return `<div class="system-row"><div class="system-name">${{displayedName}}</div>${{separation}}${{facts.properties ? `<div class="system-properties">${{escapeHtml(facts.properties)}}</div>` : ""}}${{facts.semantic || facts.pm ? `<div class="muted">${{escapeHtml(facts.semantic || "unknown")}}${{facts.pm}}</div>` : ""}}${{footer ? `<div class="muted">${{escapeHtml(footer)}}</div>` : ""}}</div>`;
      }};
      const relativeRows = relatives.map(row => {{
        const references = row.bibcodes && row.bibcodes.length ? ` · ${{row.bibcodes.length}} reference${{row.bibcodes.length === 1 ? "" : "s"}}` : "";
        const status = `${{row.direction}} · ${{relativeActionLabel(row.action)}} · ${{relevanceLabel(row.component_relevance)}}${{references}}`;
        if (row.action === "context_only") {{
          return `<div class="system-row"><div class="system-name">${{escapeHtml(row.main_id)}}</div><div class="muted">${{escapeHtml(status)}}</div></div>`;
        }}
        const target = row.matched_sdbid ? targetBySdbid[row.matched_sdbid] : null;
        if (target) return targetRow(target, row, status);
        const facts = targetFacts(null, row);
        const separation = Number(row.separation_arcsec) > 0.005
          ? `<div class="muted">${{displayNumber(row.separation_arcsec)}}" from target</div>`
          : "";
        return `<div class="system-row"><div class="system-name">${{escapeHtml(row.main_id)}}</div>${{separation}}${{facts.properties ? `<div class="system-properties">${{escapeHtml(facts.properties)}}</div>` : ""}}<div class="muted">${{escapeHtml(status)}}</div></div>`;
      }}).join("");
      const targetRows = otherTargets.map(target => targetRow(target, null)).join("");
      const crossItems = cross.map(row => {{
        const linked = (row.matched_nearby_targets || []).map(target => target.sdbid).join(", ");
        return `<li><code>${{escapeHtml(row.provider)}} ${{escapeHtml(row.source_id)}}</code> ${{row.accepted ? "accepted" : "rejected"}} <span class="muted">${{displayNumber(row.separation_arcsec)}}"</span> → ${{escapeHtml(linked)}}</li>`;
      }}).join("");
      const catalogCrossItems = catalogCross
        .filter(row => (
          ["strong_candidate", "accepted", "rejected"].includes(row.association_status)
          && (
            row.target_sdbid === view.sdbid
            || (row.encounter_sdbids || []).includes(view.sdbid)
          )
        ))
        .map(row => {{
          const source = row.source_display_name || row.source_id;
          const encounter = (row.encounter_sdbids || []).map(
            value => targetMainId(value)
          ).join(", ");
          const decision = row.association_status === "strong_candidate"
            ? ""
            : ` · ${{row.association_status}}`;
          return `<li><code>${{escapeHtml(row.provider)}} ${{escapeHtml(source)}}</code> <span class="muted">${{displayNumber(row.separation_arcsec)}}"${{decision}}</span> → ${{targetReviewLink(row.target_sdbid, targetMainId(row.target_sdbid))}} <span class="muted">(${{escapeHtml(row.association_basis)}}; encountered by ${{escapeHtml(encounter)}})</span></li>`;
        }}).join("");
      const matrixSymbol = status => ({{agrees: "✓", proposed: "+", current_only: "●", differs: "!", candidate: "·", empty: ""}})[status] || "";
      const matrixHeader = (matrix.columns || []).map(column => `<th title="${{escapeHtml(column.sdbid)}} · ${{escapeHtml(column.role)}}/${{escapeHtml(column.state)}}">${{escapeHtml(column.label)}}<br><span class="muted">${{escapeHtml(column.role)}}</span></th>`).join("");
      const matrixRows = (matrix.rows || []).map(row => {{
        const cells = (row.cells || []).map(cell => {{
          const details = [`status ${{cell.status}}`];
          if (row.proposal_confidence) details.push(`proposal confidence ${{row.proposal_confidence}}`);
          if (row.proposal_reason) details.push(row.proposal_reason);
          if (row.comparison_to_current) details.push(row.comparison_to_current);
          if (cell.current_roles && cell.current_roles.length) details.push(`current ${{cell.current_roles.join(", ")}}`);
          if (cell.proposed_roles && cell.proposed_roles.length) details.push(`proposed ${{cell.proposed_roles.join(", ")}}`);
          if (cell.mixed_band_assignments) details.push("bands have different assignments");
          const bandStates = Object.entries(cell.band_statuses || {{}}).map(([band, status]) => `${{band}}: ${{status}}`);
          if (bandStates.length) details.push(`band states ${{bandStates.join(", ")}}`);
          if (cell.separation_arcsec != null) details.push(`${{displayNumber(cell.separation_arcsec)}} arcsec`);
          if (cell.identifier_match) details.push("identifier match");
          if (cell.duplicate_proposal_conflict) details.push("duplicate stored rows propose different ownership");
          return `<td class="matrix-cell ${{cell.status}}" title="${{escapeHtml(details.join(" · "))}}">${{matrixSymbol(cell.status)}}</td>`;
        }}).join("");
        const bands = row.bands || [];
        const bandNames = bands.map(value => value.band);
        const bandCount = row.band_count || bandNames.length;
        const duplicate = row.stored_measurement_count > bandCount ? ` · ${{row.stored_measurement_count}} stored measurements${{row.duplicate_proposal_conflict ? " ⚠" : ""}}` : "";
        const mixed = row.mixed_band_assignments ? ' · <span class="matrix-warning">mixed assignments</span>' : "";
        const encounters = (row.encounter_sdbids || []).join(", ");
        const encounterText = encounters ? `<br><span class="muted">seen by ${{escapeHtml(encounters)}}</span>` : "";
        const proposalDetails = [`${{row.provider}} detection`, `ID ${{row.source_display_name || row.source_id}}`];
        if (bandNames.length) proposalDetails.push(`bands ${{bandNames.join(", ")}}`);
        for (const band of bands) {{
          proposalDetails.push(`${{band.band}}: ${{band.comparison_to_current}}${{band.excluded ? "; excluded" : ""}}`);
        }}
        if (row.proposal_confidence) proposalDetails.push(`proposal confidence ${{row.proposal_confidence}}`);
        if (row.catalog_component && row.catalog_component.native_code) {{
          const componentLabel = row.catalog_component.component_label
            ? ` → ${{row.catalog_component.component_label}}`
            : "";
          proposalDetails.push(`catalog component ${{row.catalog_component.native_code}} (${{row.catalog_component.kind}}${{componentLabel}})`);
        }}
        if (row.proposal_reason) proposalDetails.push(row.proposal_reason);
        if (row.comparison_to_current) proposalDetails.push(row.comparison_to_current);
        const sourceName = row.source_display_name || row.source_id;
        const bandSummary = `${{bandCount}} band${{bandCount === 1 ? "" : "s"}}${{bandNames.length ? `: ${{bandNames.join(", ")}}` : ""}}`;
        const componentSummary = row.catalog_component && row.catalog_component.component_label
          ? ` · component ${{row.catalog_component.component_label}}`
          : "";
        return `<tr><td><code>${{escapeHtml(row.provider)}}</code> <span class="matrix-info" title="${{escapeHtml(proposalDetails.join(" · "))}}">ⓘ</span><br><span class="matrix-source">${{sourceLink(sourceName, row.provenance)}}</span><span class="muted">${{escapeHtml(bandSummary + componentSummary)}}${{duplicate}}${{mixed}}</span>${{encounterText}}</td>${{cells}}</tr>`;
      }}).join("");
      const matrixHtml = matrixRows ? `<div class="matrix-wrap"><table class="assignment-matrix"><thead><tr><th>detection</th>${{matrixHeader}}</tr></thead><tbody>${{matrixRows}}</tbody></table></div><div class="muted">✓ current agrees · + proposed · ● current only · ! differs or mixed · · candidate</div>` : '<div class="muted">No current measurements.</div>';
      const relativeChanges = relatives.some(row => row.action === "import" || row.action === "reconcile");
      const relativeControl = document.body.classList.contains("embedded") && relatives.length
        ? `<button id="review-relatives" class="relative-review" type="button">${{relativeChanges ? "Review or reconcile SIMBAD relatives" : "View SIMBAD relatives"}}</button>`
        : "";
      const radiusSummary = `${{nearbyTargetCount}} nearby SDB target${{nearbyTargetCount === 1 ? "" : "s"}}`;
      const radiusControl = document.body.classList.contains("embedded")
        ? `<form id="system-context-radius-form" class="system-radius-control"><label for="system-context-radius">Radius</label><input id="system-context-radius" type="number" min="1" max="600" step="1" value="${{escapeHtml(String(context.radius_arcsec))}}"><span>arcsec</span><button type="submit">Apply</button><span class="muted">${{radiusSummary}}</span></form>`
        : `<div class="muted">radius ${{displayNumber(context.radius_arcsec)}}" · ${{radiusSummary}}</div>`;
      currentTargetElement.innerHTML = currentTarget
        ? targetRow(currentTarget, null, "", false)
        : '<div class="muted">Unavailable.</div>';
      element.innerHTML = `
        ${{radiusControl}}
        <h3>Immediate SIMBAD relatives</h3>
        ${{relativeRows || '<div class="muted">None or no current SIMBAD metadata.</div>'}}
        ${{relativeControl}}
        <h3>Other nearby SDB targets</h3>
        ${{targetRows || '<div class="muted">None.</div>'}}
        <h3>Identity cross-candidates</h3>
        <ul class="system-list">${{crossItems || '<li class="muted">None.</li>'}}</ul>
        <h3>Catalog cross-candidates</h3>
        <ul class="system-list">${{catalogCrossItems || '<li class="muted">None.</li>'}}</ul>
      `;
      const radiusForm = document.getElementById("system-context-radius-form");
      if (radiusForm) radiusForm.addEventListener("submit", event => {{
        event.preventDefault();
        const input = document.getElementById("system-context-radius");
        const radius = Number(input.value);
        if (!Number.isFinite(radius) || radius < 1 || radius > 600) {{
          input.setCustomValidity("Enter a radius between 1 and 600 arcsec.");
          input.reportValidity();
          return;
        }}
        input.setCustomValidity("");
        const url = new URL(window.location.href);
        url.searchParams.set("radius", String(radius));
        window.location.assign(url);
      }});
      const relativeButton = document.getElementById("review-relatives");
      if (relativeButton) relativeButton.addEventListener("click", () => {{
        window.parent.postMessage({{type: "sdb-review-relatives"}}, window.location.origin);
      }});
      photometryElement.innerHTML = `<div class="matrix-heading"><h3>System photometry matrix</h3><button id="toggle-review-drawer" class="review-drawer-toggle" type="button" aria-pressed="false">Show review tools</button></div>${{matrixHtml}}`;
      const drawerToggle = document.getElementById("toggle-review-drawer");
      drawerToggle.addEventListener("click", () => {{
        window.parent.postMessage(
          {{type: "sdb-review-drawer-toggle", visible: !reviewDrawerVisible}},
          window.location.origin,
        );
      }});
      updateReviewDrawerToggle(reviewDrawerVisible);
    }}
    function updateReviewDrawerToggle(visible) {{
      reviewDrawerVisible = Boolean(visible);
      const button = document.getElementById("toggle-review-drawer");
      if (!button) return;
      button.setAttribute("aria-pressed", String(reviewDrawerVisible));
      button.textContent = reviewDrawerVisible ? "Hide review tools" : "Show review tools";
    }}
    window.addEventListener("message", event => {{
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== "sdb-review-drawer-state") return;
      updateReviewDrawerToggle(event.data.visible);
    }});
    function showDetails(point) {{
      const pm = point.pm_ra_cosdec_masyr == null || point.pm_dec_masyr == null ? "" : `${{displayNumber(point.pm_ra_cosdec_masyr)}}, ${{displayNumber(point.pm_dec_masyr)}} mas/yr (${{point.pm_source || "unknown"}})`;
      const uncertainty = point.uncertainty_major_arcsec == null ? "" : `${{displayNumber(point.uncertainty_major_arcsec)}} × ${{displayNumber(point.uncertainty_minor_arcsec ?? point.uncertainty_major_arcsec)}} arcsec`;
      const shortRows = [["provider", point.provider], ["status", point.status], ["separation", `${{displayNumber(point.separation_arcsec)}} arcsec`], ["score", point.score == null ? "" : displayNumber(point.score)], ["offset", `${{displayNumber(point.x_arcsec)}}\" east, ${{displayNumber(point.y_arcsec)}}\" north`], ["native epoch", point.native_epoch == null ? "" : displayNumber(point.native_epoch)], ["display epoch", point.display_epoch == null ? "" : displayNumber(point.display_epoch)], ["kind", point.kind], ["accepted", point.accepted ? "yes" : "no"], ["target ID", point.target_id ?? ""], ["detection ID", point.detection_id ?? ""], ["run ID", point.run_id ?? ""], ["raw row ID", point.raw_row_id ?? ""], ["candidate ID", point.candidate_id ?? ""]];
      const longRows = [["ID", sourceLink(pointDisplayId(point), point.provenance), true], ["catalog component", point.catalog_component || ""], ["catalog query target", point.run_target_sdbid ? targetMainId(point.run_target_sdbid) : ""], ["linked targets", (point.linked_target_sdbids || []).map(value => targetMainId(value)).join("; ")], ["cross-match reason", point.cross_candidate_reason || ""], ["photometry", listValue(point.photometry)], ["photometry beams", beamValue(point.photometry_beams)], ["attributes", listValue(point.attributes)], ["proper motion", pm], ["position uncertainty", uncertainty], ["note", point.note || ""]];
      const column = rows => `<dl class="detail-list">${{rows.filter(([,value]) => value !== "" && value != null).map(([key,value,html]) => `<div class="detail-row"><dt><code>${{escapeHtml(String(key))}}</code></dt><dd>${{html ? value : escapeHtml(String(value))}}</dd></div>`).join("")}}</dl>`;
      document.getElementById("details").innerHTML = `<div class="details-columns">${{column(shortRows)}}${{column(longRows)}}</div>`;
    }}
    function clearDetails() {{ document.getElementById("details").innerHTML = "Click a point in the sky view or plotted item list."; }}
    function updateBeamVisibility() {{
      const plot = document.getElementById("sky");
      for (let traceIndex = 0; traceIndex < plot.data.length; traceIndex++) {{
        const trace = plot.data[traceIndex];
        if (trace.meta && trace.meta.review_kind === "beam") {{
          const visible = beamsVisible && selectedPointIndex != null && trace.meta.point_index === selectedPointIndex;
          Plotly.restyle(plot, {{visible: visible}}, [traceIndex]);
        }}
      }}
    }}
    function applySelection(index) {{
      selectedPointIndex = index;
      renderPointList();
      const plot = document.getElementById("sky");
      for (let traceIndex = 0; traceIndex < plot.data.length; traceIndex++) {{
        const trace = plot.data[traceIndex];
        const kind = trace.meta && trace.meta.review_kind;
        if (kind === "points") {{
          const opacities = trace.customdata.map(pointIndex => index == null ? defaultPointOpacity(view.points[pointIndex]) : (pointIndex === index ? 1.0 : 0.14));
          Plotly.restyle(plot, {{"marker.opacity": [opacities]}}, [traceIndex]);
        }} else if (kind === "line") {{
          const opacity = index == null ? defaultLineOpacity(trace) : (trace.meta.point_index === index ? 0.95 : 0.14);
          Plotly.restyle(plot, {{opacity: opacity}}, [traceIndex]);
        }}
      }}
      updateBeamVisibility();
      for (const row of document.querySelectorAll(".tree-link")) {{
        const rowIndex = row.dataset.pointIndex === "" ? null : Number(row.dataset.pointIndex);
        row.classList.toggle("selected", index != null && rowIndex === index);
        row.classList.toggle("dimmed", index != null && rowIndex !== index);
      }}
      if (index == null) clearDetails();
      if (window.parent !== window) {{
        window.parent.postMessage(
          {{type: "sdb-review-selection", point: index == null ? null : view.points[index]}},
          window.location.origin,
        );
      }}
    }}
    document.getElementById("sky").on("plotly_click", event => {{
      clickedPlotItem = true;
      const point = event.points[0];
      const index = point?.customdata ?? point?.data?.meta?.point_index;
      if (index != null) {{
        applySelection(index);
        showDetails(view.points[index]);
      }}
    }});
    document.getElementById("sky").addEventListener("click", () => {{
      setTimeout(() => {{
        if (!clickedPlotItem) applySelection(null);
        clickedPlotItem = false;
      }}, 0);
    }});
    const points = document.getElementById("points");
    const memberships = (view.system_context && view.system_context.system_memberships_by_target) || {{}};
    const requestedSystemIds = new Set(
      (memberships[view.sdbid] || []).map(row => String(row.system_id))
    );
    const explicitSystemMembers = new Set(
      Object.entries(memberships)
        .filter(([, rows]) => rows.some(row => requestedSystemIds.has(String(row.system_id))))
        .map(([sdbid]) => sdbid)
    );
    function pointIsDefaultRelevant(point) {{
      if (point.status === "target") return true;
      if (point.accepted || ["ambiguous", "transient_failure", "permanent_failure"].includes(point.status)) return true;
      if (point.linked_target_sdbids && point.linked_target_sdbids.length) return true;
      return point.provider === "sdb" && explicitSystemMembers.has(point.source_id);
    }}
    function renderPointList() {{
      points.innerHTML = "";
      const ordered = [...view.points].sort((a, b) => a.separation_arcsec - b.separation_arcsec || a.provider.localeCompare(b.provider) || a.source_id.localeCompare(b.source_id));
      const visible = ordered.filter(point => pointListExpanded || pointIsDefaultRelevant(point) || point.index === selectedPointIndex);
      for (const point of visible) {{
        const row = document.createElement("div");
        const effectivelyAccepted = (
          point.status === "accepted"
          || (point.accepted && point.status !== "rejected")
        );
        row.className = "point-row" + (effectivelyAccepted ? " accepted" : "");
        row.dataset.pointIndex = point.index;
        row.classList.toggle("selected", selectedPointIndex != null && point.index === selectedPointIndex);
        row.classList.toggle("dimmed", selectedPointIndex != null && point.index !== selectedPointIndex);
        const color = pointColor(point);
        const linked = linkedTargetValue(point);
        const runTarget = catalogRunTargetValue(point);
        const borderColor = point.linked_target_sdbids && point.linked_target_sdbids.length ? "#7c3aed" : color;
        const content = point.provider === "sdb"
          ? `<code>${{escapeHtml(point.provider)}}</code> ${{escapeHtml(point.status)}} ${{sourceLink(pointDisplayId(point), point.provenance)}}${{linked ? `<span class="muted">${{escapeHtml(linked)}}</span>` : ""}}${{runTarget ? `<span class="muted">${{escapeHtml(runTarget)}}</span>` : ""}} <span class="muted">${{displayNumber(point.separation_arcsec)}}\"</span>`
          : `<span class="point-primary"><code>${{escapeHtml(point.provider)}}</code> ${{sourceLink(pointDisplayId(point), point.provenance)}} <span class="muted">${{displayNumber(point.separation_arcsec)}}\"</span></span><span class="point-secondary">${{escapeHtml(String(point.status).replaceAll("_", " "))}}${{linked ? `<span class="muted">${{escapeHtml(linked)}}</span>` : ""}}${{runTarget ? `<span class="muted">${{escapeHtml(runTarget)}}</span>` : ""}}</span>`;
        row.innerHTML = `<span class="swatch" style="background:${{point.status === "no_match" ? "transparent" : color}}; border-color:${{borderColor}}"></span><span>${{content}}</span>`;
        row.addEventListener("click", () => {{ applySelection(point.index); showDetails(point); }});
        points.appendChild(row);
      }}
      const hiddenCount = ordered.length - ordered.filter(point => pointIsDefaultRelevant(point)).length;
      document.getElementById("toggle-point-list").textContent = pointListExpanded ? "Show relevant items" : `Show all plotted items (${{hiddenCount}} more)`;
      document.getElementById("point-list-summary").textContent = pointListExpanded ? `${{ordered.length}} items` : `${{visible.length}} relevant item${{visible.length === 1 ? "" : "s"}}`;
    }}
    document.getElementById("toggle-point-list").addEventListener("click", () => {{
      pointListExpanded = !pointListExpanded;
      renderPointList();
    }});
    renderPointList();
    const tree = document.getElementById("hierarchy-tree");
    if (view.hierarchy_tree && view.hierarchy_tree.length) {{
      tree.classList.remove("muted");
      tree.innerHTML = "";
      const componentTargets = componentStatusMap();
      for (const group of view.hierarchy_tree) {{
        const wrapper = document.createElement("div");
        wrapper.className = "tree-group";
        const title = document.createElement("div");
        title.className = "tree-title";
        const providerHtml = group.access_url
          ? `<a href="${{escapeHtml(group.access_url)}}" target="_blank" rel="noopener">${{escapeHtml(group.provider)}}</a>`
          : escapeHtml(group.provider);
        title.innerHTML = `<code>${{providerHtml}}</code> ${{escapeHtml(group.native_id)}}`;
        wrapper.appendChild(title);
        for (const link of group.links) {{
          const componentTarget = componentTargets[componentKey(group.provider, group.native_id, link.component_label)] || null;
          const row = document.createElement("div");
          row.className = "tree-link";
          row.dataset.pointIndex = link.point_index == null ? "" : String(link.point_index);
          row.title = link.note || "";
          const targetHtml = componentTargetHtml(componentTarget);
          const structural = link.structural_role && link.structural_role !== "non_structural" ? ` · ${{String(link.structural_role).replaceAll("_", " ")}}` : "";
          row.innerHTML = `<span><strong>${{escapeHtml(String(link.component_label))}}</strong> <span class="muted">relative to ${{escapeHtml(String(link.reference_label))}}</span></span>${{targetHtml ? `<br><span>${{targetHtml}}</span>` : ""}}<br><span class="muted">${{escapeHtml(String(link.status).replaceAll("_", " "))}} · ${{escapeHtml(String(link.relation_type || "component").replaceAll("_", " "))}}${{escapeHtml(structural)}}</span>`;
          if (link.point_index != null) {{
            row.addEventListener("click", () => {{ applySelection(link.point_index); showDetails(view.points[link.point_index]); }});
          }}
          wrapper.appendChild(row);
        }}
        tree.appendChild(wrapper);
      }}
    }}
    renderSystemContext();
    if (window.parent !== window) {{
      window.parent.postMessage(
        {{type: "sdb-review-drawer-ready"}},
        window.location.origin,
      );
    }}
    let skyResizeRetry = null;
    function resizeSkySquare() {{
      const plot = document.getElementById("sky");
      if (!plot._fullLayout) {{
        clearTimeout(skyResizeRetry);
        skyResizeRetry = setTimeout(resizeSkySquare, 50);
        return;
      }}
      const width = Math.round(plot.parentElement.getBoundingClientRect().width);
      const bounds = plot.getBoundingClientRect();
      const layoutWidth = Number(plot._fullLayout.width || 0);
      const layoutHeight = Number(plot._fullLayout.height || 0);
      if (width > 0 && (Math.abs(bounds.width - width) > 1 || Math.abs(bounds.height - width) > 1 || Math.abs(layoutWidth - width) > 1 || Math.abs(layoutHeight - width) > 1)) {{
        Plotly.relayout(plot, {{width: width, height: width}});
      }}
    }}
    const skyResizeObserver = new ResizeObserver(() => requestAnimationFrame(resizeSkySquare));
    skyResizeObserver.observe(document.querySelector(".plot-column"));
    setTimeout(resizeSkySquare, 0);
    window.addEventListener("resize", resizeSkySquare);
    document.getElementById("toggle-annotations").addEventListener("click", event => {{
      annotationsVisible = !annotationsVisible;
      Plotly.relayout("sky", {{annotations: annotationsVisible ? labelAnnotations : []}});
      event.target.textContent = annotationsVisible ? "Hide target labels" : "Show target labels";
    }});
    document.getElementById("toggle-beams").addEventListener("click", event => {{
      beamsVisible = !beamsVisible;
      updateBeamVisibility();
      event.target.textContent = beamsVisible ? "Hide photometry beams" : "Show photometry beams";
    }});
  </script>
</body>
</html>
"""

def _view_payload(view: ReviewSkyView) -> dict[str, object]:
    points = []
    paths = []
    for index, point in enumerate(view.points):
        value = asdict(point)
        x_arcsec, y_arcsec = _offset_arcsec(
            (view.center_ra_deg, view.center_dec_deg),
            point.ra_deg,
            point.dec_deg,
        )
        value["index"] = index
        value["x_arcsec"] = x_arcsec
        value["y_arcsec"] = y_arcsec
        points.append(value)
        if (
            point.native_ra_deg is not None
            and point.native_dec_deg is not None
            and point.native_epoch is not None
            and not math.isclose(point.native_epoch, point.display_epoch)
        ):
            native_x, native_y = _offset_arcsec(
                (view.center_ra_deg, view.center_dec_deg),
                point.native_ra_deg,
                point.native_dec_deg,
            )
            if math.hypot(native_x - x_arcsec, native_y - y_arcsec) > 1e-6:
                paths.append({
                    "index": index,
                    "provider": point.provider,
                    "source_id": point.source_id,
                    "native_epoch": point.native_epoch,
                    "display_epoch": point.display_epoch,
                    "pm_source": point.pm_source,
                    "x_native_arcsec": native_x,
                    "y_native_arcsec": native_y,
                    "x_display_arcsec": x_arcsec,
                    "y_display_arcsec": y_arcsec,
                })
    arrows = []
    for arrow in view.arrows:
        value = asdict(arrow)
        x_arcsec, y_arcsec = _offset_arcsec(
            (view.center_ra_deg, view.center_dec_deg),
            arrow.ra_deg,
            arrow.dec_deg,
        )
        value["x_arcsec"] = x_arcsec
        value["y_arcsec"] = y_arcsec
        value["x_end_arcsec"] = x_arcsec + arrow.pm_ra_cosdec_masyr * arrow.years / 1000.0
        value["y_end_arcsec"] = y_arcsec + arrow.pm_dec_masyr * arrow.years / 1000.0
        value["point_index"] = _nearest_point_index(points, x_arcsec, y_arcsec, arrow.target_id)
        arrows.append(value)
    candidate_point_index = {
        point.get("candidate_id"): int(point["index"])
        for point in points
        if point.get("kind") == "hierarchy" and point.get("candidate_id") is not None
    }
    segments = []
    for segment in view.segments:
        value = asdict(segment)
        x_start, y_start = _offset_arcsec(
            (view.center_ra_deg, view.center_dec_deg),
            segment.start_ra_deg,
            segment.start_dec_deg,
        )
        x_end, y_end = _offset_arcsec(
            (view.center_ra_deg, view.center_dec_deg),
            segment.end_ra_deg,
            segment.end_dec_deg,
        )
        value["x_start_arcsec"] = x_start
        value["y_start_arcsec"] = y_start
        value["x_end_arcsec"] = x_end
        value["y_end_arcsec"] = y_end
        value["point_index"] = candidate_point_index.get(segment.candidate_id)
        segments.append(value)
    return {
        "target_id": view.target_id,
        "sdbid": view.sdbid,
        "center_ra_deg": view.center_ra_deg,
        "center_dec_deg": view.center_dec_deg,
        "radius_arcsec": view.radius_arcsec,
        "points": points,
        "arrows": arrows,
        "segments": segments,
        "paths": paths,
        "hierarchy_tree": _hierarchy_tree_payload(segments),
        "system_context": view.system_context,
    }


def _hierarchy_tree_payload(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], dict[str, object]] = {}
    for segment in segments:
        native_id = str(segment.get("native_id") or segment.get("source_id") or "")
        provider = str(segment.get("provider") or "")
        key = (provider, native_id)
        locator = _HIERARCHY_VIZIER_LOCATORS.get(provider)
        group = groups.setdefault(
            key,
            {
                "provider": provider,
                "native_id": native_id,
                "access_url": (
                    vizier_entry_url(locator[0], locator[1], native_id)
                    if locator and native_id
                    else None
                ),
                "links": [],
            },
        )
        group["links"].append({
            "status": segment.get("status"),
            "source_id": segment.get("source_id"),
            "reference_label": segment.get("reference_label") or "primary",
            "component_label": segment.get("component_label") or segment.get("label") or "component",
            "relation_type": segment.get("relation_type") or "component",
            "structural_role": segment.get("structural_role") or "non_structural",
            "label": segment.get("label"),
            "candidate_id": segment.get("candidate_id"),
            "point_index": segment.get("point_index"),
            "note": segment.get("note") or "",
        })
    result = list(groups.values())
    for group in result:
        group["links"] = sorted(
            group["links"],
            key=lambda value: (
                str(value.get("component_label") or ""),
                str(value.get("relation_type") or ""),
                str(value.get("reference_label") or ""),
                str(value.get("source_id") or ""),
            ),
        )
    return sorted(
        result,
        key=lambda value: (str(value["provider"]), str(value["native_id"])),
    )


def _nearest_point_index(
    points: list[dict[str, object]],
    x_arcsec: float,
    y_arcsec: float,
    target_id: int | None,
) -> int | None:
    best_index = None
    best_distance = math.inf
    for point in points:
        if target_id is not None and point.get("target_id") != target_id:
            continue
        distance = math.hypot(
            float(point["x_arcsec"]) - x_arcsec,
            float(point["y_arcsec"]) - y_arcsec,
        )
        if distance < best_distance:
            best_distance = distance
            best_index = int(point["index"])
    if best_distance <= 1e-3:
        return best_index
    return None


def _deduplicate_points(points: list[SkyPoint]) -> list[SkyPoint]:
    merged: dict[tuple[object, ...], SkyPoint] = {}
    order: list[tuple[object, ...]] = []
    for point in points:
        key = (
            point.provider,
            point.status,
            point.source_id,
            point.accepted,
            point.target_id,
            round(point.ra_deg, 9),
            round(point.dec_deg, 9),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = point
            order.append(key)
            continue
        merged[key] = _merge_duplicate_point(existing, point)
    return [merged[key] for key in order]


def _merge_duplicate_point(first: SkyPoint, second: SkyPoint) -> SkyPoint:
    kinds = []
    for kind in (*first.kind.split("+"), *second.kind.split("+")):
        if kind not in kinds:
            kinds.append(kind)
    notes = []
    for note in (first.note, second.note):
        if note and note not in notes:
            notes.append(note)
    photometry = tuple(dict.fromkeys((*first.photometry, *second.photometry)))
    photometry_beams = tuple(dict.fromkeys((*first.photometry_beams, *second.photometry_beams)))
    attributes = tuple(dict.fromkeys((*first.attributes, *second.attributes)))
    provenance = []
    for item in (*first.provenance, *second.provenance):
        if item not in provenance:
            provenance.append(item)
    return replace(
        first,
        kind="+".join(kinds),
        score=first.score if first.score is not None else second.score,
        run_id=first.run_id if first.run_id is not None else second.run_id,
        raw_row_id=first.raw_row_id if first.raw_row_id is not None else second.raw_row_id,
        candidate_id=first.candidate_id if first.candidate_id is not None else second.candidate_id,
        detection_id=first.detection_id if first.detection_id is not None else second.detection_id,
        target_id=first.target_id if first.target_id is not None else second.target_id,
        run_target_sdbid=first.run_target_sdbid or second.run_target_sdbid,
        native_epoch=first.native_epoch if first.native_epoch is not None else second.native_epoch,
        native_ra_deg=first.native_ra_deg if first.native_ra_deg is not None else second.native_ra_deg,
        native_dec_deg=first.native_dec_deg if first.native_dec_deg is not None else second.native_dec_deg,
        pm_ra_cosdec_masyr=first.pm_ra_cosdec_masyr if first.pm_ra_cosdec_masyr is not None else second.pm_ra_cosdec_masyr,
        pm_dec_masyr=first.pm_dec_masyr if first.pm_dec_masyr is not None else second.pm_dec_masyr,
        pm_source=first.pm_source if first.pm_source is not None else second.pm_source,
        photometry=photometry,
        photometry_beams=photometry_beams,
        attributes=attributes,
        linked_target_sdbids=tuple(dict.fromkeys((*first.linked_target_sdbids, *second.linked_target_sdbids))),
        cross_candidate_reason=first.cross_candidate_reason or second.cross_candidate_reason,
        uncertainty_major_arcsec=first.uncertainty_major_arcsec if first.uncertainty_major_arcsec is not None else second.uncertainty_major_arcsec,
        uncertainty_minor_arcsec=first.uncertainty_minor_arcsec if first.uncertainty_minor_arcsec is not None else second.uncertainty_minor_arcsec,
        catalog_component=first.catalog_component or second.catalog_component,
        provenance=tuple(provenance),
        note="; duplicate view row merged: ".join(notes),
    )


def _annotate_identity_cross_candidates(
    points: list[SkyPoint],
    system_context: dict[str, object] | None,
) -> list[SkyPoint]:
    if not system_context:
        return points
    cross_candidates = system_context.get("identity_cross_candidates") or []
    linked_by_candidate_id = {}
    linked_by_source = {}
    for row in cross_candidates:
        linked_targets = tuple(
            str(target["sdbid"])
            for target in row.get("matched_nearby_targets") or []
            if target.get("sdbid")
        )
        if not linked_targets:
            continue
        reason = (
            f"identity candidate resolves to nearby SDB target"
            f"{'s' if len(linked_targets) != 1 else ''}: {', '.join(linked_targets)}"
        )
        value = (linked_targets, reason)
        candidate_id = row.get("candidate_id")
        if candidate_id is not None:
            linked_by_candidate_id[int(candidate_id)] = value
        linked_by_source[(str(row.get("provider") or ""), str(row.get("source_id") or ""))] = value
    annotated = []
    for point in points:
        if point.kind != "identity":
            annotated.append(point)
            continue
        value = None
        if point.candidate_id is not None:
            value = linked_by_candidate_id.get(point.candidate_id)
        if value is None:
            value = linked_by_source.get((point.provider, point.source_id))
        if value is None:
            annotated.append(point)
            continue
        linked_targets, reason = value
        notes = [point.note, reason]
        annotated.append(replace(
            point,
            linked_target_sdbids=tuple(dict.fromkeys((*point.linked_target_sdbids, *linked_targets))),
            cross_candidate_reason=reason,
            note="; ".join(note for note in notes if note),
        ))
    return annotated


def _target_center(session: Session, target: Target) -> tuple[float, float]:
    solution = _target_solution(session, target)
    if solution is not None:
        return solution.derived_ra2000_deg, solution.derived_dec2000_deg
    return target.ra2000_deg, target.dec2000_deg


def _target_solution(session: Session, target: Target) -> AstrometricSolution | None:
    if target.canonical_astrometry_id is None:
        return None
    return session.get(AstrometricSolution, target.canonical_astrometry_id)


def _target_motion_solution(
    session: Session,
    target: Target,
    solution: AstrometricSolution | None = None,
) -> AstrometricSolution | Astrometry | None:
    solution = solution if solution is not None else _target_solution(session, target)
    if solution is not None and solution.proper_motion_available:
        return solution
    metadata = session.scalar(
        select(SimbadMetadata)
        .where(
            SimbadMetadata.target_id == target.id,
            SimbadMetadata.pm_ra_cosdec_masyr.is_not(None),
            SimbadMetadata.pm_dec_masyr.is_not(None),
        )
        .order_by(SimbadMetadata.id.desc())
        .limit(1)
    )
    if metadata is None:
        return solution
    return Astrometry(
        metadata.ra_deg,
        metadata.dec_deg,
        2000.0,
        pm_ra_cosdec_masyr=metadata.pm_ra_cosdec_masyr,
        pm_dec_masyr=metadata.pm_dec_masyr,
        source="simbad metadata",
        source_id=metadata.main_id,
        proper_motion_bibcode=metadata.proper_motion_bibcode,
    )


def _proper_motion_arrows(
    target: Target,
    solution: AstrometricSolution | Astrometry,
    *,
    years: float = 10.0,
) -> list[SkyArrow]:
    if (
        not solution.proper_motion_available
        or solution.pm_ra_cosdec_masyr is None
        or solution.pm_dec_masyr is None
    ):
        return []
    if isinstance(solution, AstrometricSolution):
        ra2000 = solution.derived_ra2000_deg
        dec2000 = solution.derived_dec2000_deg
    else:
        position = propagate_to_epoch(solution, 2000.0)
        ra2000 = position.ra_deg
        dec2000 = position.dec_deg
    return [
        SkyArrow(
            kind="proper_motion",
            provider=solution.source,
            source_id=solution.source_id or target.sdbid,
            ra_deg=ra2000,
            dec_deg=dec2000,
            pm_ra_cosdec_masyr=solution.pm_ra_cosdec_masyr,
            pm_dec_masyr=solution.pm_dec_masyr,
            years=years,
            target_id=target.id,
            note=f"{years:g} yr proper-motion vector",
        )
    ]


def _nearby_target_points(
    session: Session,
    target: Target,
    center: tuple[float, float],
    radius_arcsec: float,
) -> tuple[list[SkyPoint], list[SkyArrow]]:
    radius_deg = radius_arcsec / 3600.0
    dec0 = center[1]
    cos_dec = max(0.01, abs(math.cos(math.radians(dec0))))
    rows = session.scalars(
        select(Target)
        .where(Target.id != target.id)
        .where(Target.dec2000_deg.between(dec0 - radius_deg, dec0 + radius_deg))
        .where(Target.ra2000_deg.between(center[0] - radius_deg / cos_dec, center[0] + radius_deg / cos_dec))
        .order_by(Target.id)
    )
    points = []
    arrows = []
    for nearby in rows:
        separation = _separation_arcsec(center, nearby.ra2000_deg, nearby.dec2000_deg)
        if separation > radius_arcsec:
            continue
        solution = _target_solution(session, nearby)
        motion_solution = _target_motion_solution(session, nearby, solution)
        points.append(
            SkyPoint(
                kind="nearby_target",
                provider="sdb",
                status="nearby",
                source_id=nearby.sdbid,
                ra_deg=nearby.ra2000_deg,
                dec_deg=nearby.dec2000_deg,
                separation_arcsec=separation,
                target_id=nearby.id,
                pm_ra_cosdec_masyr=(
                    None
                    if motion_solution is None
                    else motion_solution.pm_ra_cosdec_masyr
                ),
                pm_dec_masyr=(
                    None if motion_solution is None else motion_solution.pm_dec_masyr
                ),
                pm_source=(
                    None if motion_solution is None else motion_solution.source
                ),
                note="nearby SDB target",
            )
        )
        if motion_solution is not None:
            arrows.extend(_proper_motion_arrows(nearby, motion_solution))
    return points, arrows


def _identity_points(
    session: Session,
    target: Target,
    center: tuple[float, float],
    *,
    motion_solution: AstrometricSolution | Astrometry | None = None,
) -> list[SkyPoint]:
    if motion_solution is None:
        motion_solution = _target_motion_solution(session, target)
    rows = session.execute(
        select(MatchCandidate, Submission)
        .join(Submission, Submission.id == MatchCandidate.submission_id)
        .where(Submission.target_id == target.id)
        .order_by(MatchCandidate.provider, MatchCandidate.score.desc(), MatchCandidate.id)
    )
    selected_ids = effective_identity_candidate_ids(
        session, target_ids=[target.id],
    )
    points = []
    for candidate, submission in rows:
        accepted = candidate.id in selected_ids
        status = "accepted" if accepted else "candidate"
        native_pm = None
        if (
            candidate.proper_motion_available
            and candidate.pm_ra_cosdec_masyr is not None
            and candidate.pm_dec_masyr is not None
        ):
            native_pm = (
                candidate.pm_ra_cosdec_masyr,
                candidate.pm_dec_masyr,
                candidate.provider,
            )
        pm_note = (
            ""
            if native_pm is not None
            else "; native candidate PM unavailable"
        )
        ra2000, dec2000, pm_ra, pm_dec, pm_source, note = _display_position_2000(
            candidate.ra_deg,
            candidate.dec_deg,
            candidate.epoch,
            motion_solution,
            native_pm=native_pm,
            base_note=f"identity candidate from submission {submission.id}{pm_note}",
        )
        points.append(
            SkyPoint(
                kind="identity",
                provider=candidate.provider,
                status=status,
                source_id=candidate.source_id,
                source_display_name=catalog_source_display_name(
                    candidate.provider, candidate.source_id
                ),
                ra_deg=ra2000,
                dec_deg=dec2000,
                separation_arcsec=_separation_arcsec(center, ra2000, dec2000),
                score=candidate.score,
                accepted=accepted,
                candidate_id=candidate.id,
                target_id=target.id,
                native_epoch=candidate.epoch,
                native_ra_deg=candidate.ra_deg,
                native_dec_deg=candidate.dec_deg,
                pm_ra_cosdec_masyr=pm_ra,
                pm_dec_masyr=pm_dec,
                pm_source=pm_source,
                note=note,
            )
        )
    return points


def _catalog_points(
    session: Session,
    target: Target,
    center: tuple[float, float],
    *,
    motion_solution: AstrometricSolution | Astrometry | None = None,
    run_statuses: set[str] | None = None,
) -> list[SkyPoint]:
    if motion_solution is None:
        motion_solution = _target_motion_solution(session, target)
    all_runs = list(session.scalars(
        select(CatalogRun)
        .where(CatalogRun.target_id == target.id)
        .order_by(CatalogRun.provider, CatalogRun.id)
    ))
    runs_by_provider: dict[str, list[CatalogRun]] = {}
    for run in all_runs:
        runs_by_provider.setdefault(run.provider, []).append(run)
    effective = effective_catalog_results(session, [target.id])
    runs: list[tuple[CatalogRun, str, frozenset[int]]] = []
    for provider_runs in runs_by_provider.values():
        current = next((run for run in provider_runs if run.is_current), None)
        latest = provider_runs[-1]
        current_result = (
            None
            if current is None
            else effective.get((target.id, current.provider))
        )
        current_status = (
            current.status
            if current_result is None
            else current_result.status.value
        ) if current is not None else None
        if current is not None and (
            run_statuses is None or current_status in run_statuses
        ):
            runs.append((
                current,
                current_status,
                frozenset(
                    raw.id
                    for raw, _detection in (
                        ()
                        if current_result is None
                        else effective_catalog_selected_rows(
                            session, current_result,
                        )
                    )
                ),
            ))
        if (
            latest.status in PROVIDER_FAILURE_STATUSES
            and (current is None or latest.id != current.id)
            and (run_statuses is None or latest.status in run_statuses)
        ):
            runs.append((latest, latest.status, frozenset()))
    runs.sort(key=lambda value: (value[0].provider, value[0].id))
    points = []
    for run, effective_status, selected_raw_row_ids in runs:
        rows = list(session.scalars(
            select(RawCatalogRow)
            .where(RawCatalogRow.run_id == run.id)
            .order_by(RawCatalogRow.score.desc(), RawCatalogRow.id)
        ))
        rows.sort(key=lambda row: row.id not in selected_raw_row_ids)
        if not rows:
            if run.status not in PROVIDER_FAILURE_STATUSES:
                continue
            ra2000, dec2000, pm_ra, pm_dec, pm_source, note = (
                _display_position_2000(
                    run.query_ra_deg,
                    run.query_dec_deg,
                    run.query_epoch,
                    motion_solution,
                    base_note=(
                        f"catalog run {run.id}; provider status {run.status}"
                        f"{f'; {run.error}' if run.error else ''}"
                    ),
                )
            )
            points.append(SkyPoint(
                kind="catalog",
                provider=run.provider,
                status=run.status,
                source_id="provider failure",
                ra_deg=ra2000,
                dec_deg=dec2000,
                separation_arcsec=_separation_arcsec(center, ra2000, dec2000),
                run_id=run.id,
                target_id=target.id,
                run_target_sdbid=target.sdbid,
                native_epoch=run.query_epoch,
                native_ra_deg=run.query_ra_deg,
                native_dec_deg=run.query_dec_deg,
                pm_ra_cosdec_masyr=pm_ra,
                pm_dec_masyr=pm_dec,
                pm_source=pm_source,
                note=note,
            ))
            continue
        for row in rows:
            payload = _catalog_payload(row.payload_json)
            association = _catalog_association(row.payload_json)
            review_only = bool(association.get("review_only"))
            accepted = row.id in selected_raw_row_ids
            status = "accepted" if accepted else (
                "review_neighbour" if review_only else (
                    "ambiguous"
                    if effective_status == "ambiguous"
                    else effective_status
                )
            )
            measurements = _measurement_summaries(session, row.id)
            beams = _measurement_beams(session, row.id)
            attributes = (
                *_catalog_payload_summaries(run.provider, row.payload_json),
                *_attribute_summaries(session, row.id),
            )
            native_pm = _attribute_pm(session, row.id, provider=run.provider)
            ra2000, dec2000, pm_ra, pm_dec, pm_source, note = _display_position_2000(
                row.ra_deg,
                row.dec_deg,
                row.epoch,
                motion_solution,
                native_pm=native_pm,
                base_note=(
                    f"catalog run {run.id}; provider status {effective_status}"
                ),
            )
            uncertainty_major, uncertainty_minor = _position_uncertainty_arcsec(
                run.provider, row.payload_json
            )
            provenance = tuple({
                "role": item.role,
                "service": item.service,
                "catalog_id": item.catalog_id,
                "table_id": item.table_id,
                "row_key": item.row_key,
                "identifier_column": item.identifier_column,
                "identifier_value": item.identifier_value,
                "source_url": item.source_url,
                "access_url": item.access_url,
                "readme_url": item.readme_url,
            } for item in session.scalars(
                select(CatalogDetectionProvenance)
                .where(CatalogDetectionProvenance.detection_id == row.detection_id)
                .order_by(CatalogDetectionProvenance.id)
            ))
            points.append(
                SkyPoint(
                    kind="catalog",
                    provider=run.provider,
                    status=status,
                    source_id=row.source_id,
                    source_display_name=catalog_source_display_name(
                        run.provider, row.source_id, payload
                    ),
                    ra_deg=ra2000,
                    dec_deg=dec2000,
                    separation_arcsec=_separation_arcsec(center, ra2000, dec2000),
                    score=row.score,
                    accepted=accepted,
                    run_id=run.id,
                    raw_row_id=row.id,
                    detection_id=row.detection_id,
                    target_id=target.id,
                    run_target_sdbid=target.sdbid,
                    native_epoch=row.epoch,
                    native_ra_deg=row.ra_deg,
                    native_dec_deg=row.dec_deg,
                    pm_ra_cosdec_masyr=pm_ra,
                    pm_dec_masyr=pm_dec,
                    pm_source=pm_source,
                    photometry=measurements,
                    photometry_beams=beams,
                    attributes=attributes,
                    uncertainty_major_arcsec=uncertainty_major,
                    uncertainty_minor_arcsec=uncertainty_minor,
                    catalog_component=_catalog_component_summary(
                        run.provider, payload, row.source_id,
                    ),
                    provenance=provenance,
                    note=note,
                )
            )
    return points


def _catalog_component_summary(
    provider: str,
    payload: dict[str, object],
    source_id: str,
) -> str | None:
    if provider == "ubvmeans":
        value = decode_ubv_component(payload, source_id)
    elif provider == "tdsc":
        value = decode_tdsc_component(payload, source_id)
    elif provider == "v70a":
        value = decode_v70a_component(payload, source_id)
    else:
        return None
    if value.kind == "named_component":
        if provider == "v70a":
            return (
                f"{value.native_code} — V/70A component "
                f"{value.component_label}"
            )
        return (
            f"{value.native_code} — TDSC component {value.component_label}; "
            "WDS designation where available"
        )
    if value.kind == "component_ordinal":
        return (
            f"{value.native_code} — component {value.component_label} "
            "(ordinal catalogue code)"
        )
    if value.kind == "combined_components":
        return "D — combined light from at least two components; subset unspecified"
    if value.kind == "supplementary_identifier":
        return "S — supplementary identification; component requires review"
    if value.kind == "unknown":
        return f"{value.native_code} — unknown catalogue component code"
    return None


def _annotate_catalog_target_candidates(
    session: Session,
    target: Target,
    center: tuple[float, float],
    points: list[SkyPoint],
    *,
    system_context: dict[str, object],
) -> list[SkyPoint]:
    """Annotate or project detections that reconcile to review targets."""
    candidate_rows = system_context.get("catalog_target_candidates") or []
    if not candidate_rows:
        return points
    strong_by_detection: dict[int, list[dict[str, object]]] = {}
    current_target_rows: dict[int, dict[str, object]] = {}
    for row in candidate_rows:
        detection_id = int(row["detection_id"])
        if row.get("target_sdbid") == target.sdbid:
            # Even an ordinary positional candidate must be actionable.  The
            # source association is the normal ownership decision; the
            # provider-run status only records how the detection was found.
            current_target_rows[detection_id] = row
        if row.get("association_status") not in {
            "current_match", "strong_candidate", "accepted", "rejected",
        }:
            continue
        strong_by_detection.setdefault(detection_id, []).append(row)

    annotated = []
    existing_detection_ids = set()
    for point in points:
        if point.detection_id is None:
            annotated.append(point)
            continue
        existing_detection_ids.add(point.detection_id)
        associations = strong_by_detection.get(point.detection_id, [])
        current_row = current_target_rows.get(point.detection_id)
        if not associations and current_row is None:
            annotated.append(point)
            continue
        linked = tuple(dict.fromkeys(
            str(row["target_sdbid"]) for row in associations
        ))
        reason = _catalog_candidate_reason(
            associations or ([current_row] if current_row is not None else [])
        )
        annotated.append(replace(
            point,
            kind=(
                "catalog_association"
                if current_row is not None
                else point.kind
            ),
            status=(
                str(current_row["association_status"])
                if (
                    current_row is not None
                    and current_row["association_status"]
                    in {"accepted", "rejected"}
                )
                else point.status
            ),
            linked_target_sdbids=tuple(dict.fromkeys(
                (*point.linked_target_sdbids, *linked)
            )),
            cross_candidate_reason=reason,
            note="; ".join(value for value in (point.note, reason) if value),
        ))

    targets = {
        value.id: value
        for value in session.scalars(select(Target).where(
            Target.id.in_({
                int(row["target_id"]) for row in current_target_rows.values()
            })
        ))
    }
    for detection_id, row in current_target_rows.items():
        if detection_id in existing_detection_ids:
            continue
        association_target = targets.get(int(row["target_id"]))
        if association_target is None:
            continue
        motion_solution = _target_motion_solution(session, association_target)
        ra2000, dec2000, pm_ra, pm_dec, pm_source, note = (
            _display_position_2000(
                float(row["ra_deg"]),
                float(row["dec_deg"]),
                float(row["epoch"]),
                motion_solution,
                base_note=(
                    f"catalog detection {detection_id}; "
                    f"{row['association_status']} for {target.sdbid}"
                ),
            )
        )
        encounter_sdbids = tuple(str(value) for value in row.get(
            "encounter_sdbids", []
        ))
        representative_raw_row_id = int(row["representative_raw_row_id"])
        reason = _catalog_candidate_reason([row])
        annotated.append(SkyPoint(
            kind="catalog_association",
            provider=str(row["provider"]),
            status=(
                str(row["association_status"])
                if row["association_status"] in {"accepted", "rejected"}
                else "candidate"
            ),
            source_id=str(row["source_id"]),
            source_display_name=str(row["source_display_name"]),
            ra_deg=ra2000,
            dec_deg=dec2000,
            separation_arcsec=_separation_arcsec(center, ra2000, dec2000),
            score=float(row["score"]),
            run_id=int(row["representative_run_id"]),
            raw_row_id=representative_raw_row_id,
            detection_id=detection_id,
            target_id=target.id,
            run_target_sdbid=(
                None
                if not encounter_sdbids
                else str(row.get("representative_run_target_sdbid") or "")
            ),
            native_epoch=float(row["epoch"]),
            native_ra_deg=float(row["ra_deg"]),
            native_dec_deg=float(row["dec_deg"]),
            pm_ra_cosdec_masyr=pm_ra,
            pm_dec_masyr=pm_dec,
            pm_source=pm_source,
            photometry=_measurement_summaries(
                session, representative_raw_row_id
            ),
            photometry_beams=_measurement_beams(
                session, representative_raw_row_id
            ),
            linked_target_sdbids=(target.sdbid,),
            cross_candidate_reason=reason,
            note="; ".join(value for value in (
                note,
                f"encountered by {', '.join(encounter_sdbids)}"
                if encounter_sdbids else "",
                reason,
            ) if value),
        ))
    return annotated


def _catalog_candidate_reason(
    rows: list[dict[str, object]],
) -> str:
    targets = [
        f"{row['target_sdbid']} ({row['association_basis']}; "
        f"{float(row['separation_arcsec']):.3f} arcsec)"
        for row in rows
    ]
    return "catalog detection reconciles to " + ", ".join(targets)


def _hierarchy_points(
    session: Session,
    target: Target,
    center: tuple[float, float],
) -> tuple[list[SkyPoint], list[SkySegment]]:
    matched_rows = list(session.execute(
        select(HierarchyMatchCandidate, HierarchyRecord)
        .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
        .where(HierarchyMatchCandidate.target_id == target.id)
        .order_by(
            HierarchyMatchCandidate.provider,
            HierarchyRecord.native_id,
            HierarchyRecord.component,
            HierarchyMatchCandidate.score.desc(),
            HierarchyMatchCandidate.id,
        )
    ))
    record_keys = {
        (record.source_id, record.native_id)
        for _candidate, record in matched_rows
    }
    if record_keys:
        source_ids = {source_id for source_id, _native_id in record_keys}
        native_ids = {native_id for _source_id, native_id in record_keys}
        sibling_records = tuple(session.scalars(
            select(HierarchyRecord)
            .where(HierarchyRecord.source_id.in_(source_ids))
            .where(HierarchyRecord.native_id.in_(native_ids))
            .order_by(HierarchyRecord.source_id, HierarchyRecord.native_id, HierarchyRecord.component)
        ))
    else:
        sibling_records = ()
    record_index = {
        (record.source_id, record.native_id, record.component): record
        for record in sibling_records
        if (record.source_id, record.native_id) in record_keys
    }
    matched_record_ids = {record.id for _candidate, record in matched_rows}
    rows: list[tuple[HierarchyMatchCandidate | None, HierarchyRecord]] = [
        *matched_rows,
        *(
            (None, record)
            for record in sibling_records
            if record.id not in matched_record_ids
            and (record.source_id, record.native_id) in record_keys
        ),
    ]
    record_ids = [record.id for _candidate, record in rows]
    graph_edges_by_record: dict[int, list] = {}
    if record_ids:
        graph_edges = tuple(session.scalars(
            select(StructuralEdge)
            .where(StructuralEdge.record_id.in_(record_ids))
            .where(StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES))
            .order_by(
                StructuralEdge.source,
                StructuralEdge.native_id,
                StructuralEdge.reference_label,
                StructuralEdge.component_label,
                StructuralEdge.id,
            )
        ))
        graph_overrides = _latest_graph_overrides(session, list(graph_edges))
        for edge in graph_edges:
            if edge.record_id is not None:
                graph_edges_by_record.setdefault(edge.record_id, []).append(
                    _graph_edge_row(edge, graph_overrides.get(edge.id))
                )
    points: list[SkyPoint] = []
    segments: list[SkySegment] = []
    for candidate, record in rows:
        if record.ra_deg is None or record.dec_deg is None:
            continue
        if _wds_record_has_unusable_separation(record):
            continue
        source_id = _hierarchy_source_id(record)
        display_ra, display_dec, display_position_kind = _hierarchy_display_position(record, center)
        status = candidate.status if candidate is not None else "context"
        candidate_id = candidate.id if candidate is not None else None
        candidate_target_id = target.id if candidate is not None else None
        note_parts = [
            (
                f"hierarchy candidate {candidate.id}"
                if candidate is not None
                else "same hierarchy group; context only"
            ),
            *(
                (f"method {candidate.match_method}",)
                if candidate is not None
                else ()
            ),
            f"record {record.id}",
            f"plotted at {display_position_kind}",
        ]
        if record.discoverer_id:
            note_parts.append(f"discoverer {record.discoverer_id}")
        if record.component:
            note_parts.append(f"component {record.component}")
        if record.measure_epoch is not None:
            note_parts.append(f"epoch {record.measure_epoch:g}")
        if record.separation_arcsec is not None and not _hierarchy_geometry_usable(record):
            note_parts.append(f"rho {record.separation_arcsec:g}\" ignored as unusable WDS sentinel")
        elif record.separation_arcsec is not None:
            note_parts.append(f"rho {record.separation_arcsec:g}\"")
        if record.pa_deg is not None:
            note_parts.append(f"PA {record.pa_deg:g} deg")
        if candidate is not None and candidate.reason:
            note_parts.append(candidate.reason)
        raw_payload = _hierarchy_raw_payload(record)
        unusable_separation = raw_payload.get("unusable_separation_arcsec")
        if unusable_separation is not None:
            note_parts.append(f"rho {float(unusable_separation):g}\" ignored as unusable WDS sentinel")
        raw_component = str(raw_payload.get("Comp") or "").strip()
        raw_reference = str(raw_payload.get("rComp") or "").strip()
        if _wds_blank_component_implies_ab(record, record_index, raw_reference, raw_component):
            raw_reference = "A"
            raw_component = "B"
            note_parts.append("blank WDS component displayed as implicit A-B pair")
        display_reference, display_component = _hierarchy_display_components(
            record.provider,
            raw_reference,
            raw_component,
            record.component,
        )
        display_source_id = _hierarchy_source_id(
            record,
            component_override=display_component if display_component else None,
        )
        if raw_reference:
            note_parts.append(f"relative to component {raw_reference}")
        if display_component and display_component != raw_component:
            note_parts.append(f"displayed endpoint component {display_component}")
        points.append(
            SkyPoint(
                kind="hierarchy",
                provider=record.provider,
                status=status,
                source_id=display_source_id,
                ra_deg=display_ra,
                dec_deg=display_dec,
                separation_arcsec=_separation_arcsec(center, display_ra, display_dec),
                score=candidate.score if candidate is not None else None,
                accepted=status == "accepted",
                candidate_id=candidate_id,
                target_id=candidate_target_id,
                attributes=tuple(_hierarchy_attribute_summaries(record)),
                note="; ".join(note_parts),
            )
        )
        graph_rows = graph_edges_by_record.get(record.id, [])
        if graph_rows:
            for graph_row in graph_rows:
                if (
                    graph_row.start_ra_deg is None
                    or graph_row.start_dec_deg is None
                    or graph_row.end_ra_deg is None
                    or graph_row.end_dec_deg is None
                ):
                    continue
                label = graph_row.component_label or graph_row.source_component or record.component or source_id
                segment_note = (
                    f"{label}: persisted hierarchy graph edge from {graph_row.reference_label or 'primary'}; "
                    f"type {graph_row.relation_type}; role {graph_row.structural_role}; geometry {graph_row.geometry_status}"
                )
                if graph_row.override_id is not None:
                    segment_note += f"; override {graph_row.override_id} by {graph_row.override_actor}: {graph_row.override_reason}"
                segments.append(
                    SkySegment(
                        kind="hierarchy_component_link",
                        provider=graph_row.provider,
                        status=graph_row.status,
                        source_id=display_source_id,
                        label=label,
                        start_ra_deg=graph_row.start_ra_deg,
                        start_dec_deg=graph_row.start_dec_deg,
                        end_ra_deg=graph_row.end_ra_deg,
                        end_dec_deg=graph_row.end_dec_deg,
                        candidate_id=candidate_id,
                        target_id=candidate_target_id,
                        native_id=graph_row.native_id,
                        reference_label=graph_row.reference_label,
                        component_label=graph_row.component_label,
                        relation_type=graph_row.relation_type,
                        structural_role=graph_row.structural_role,
                        note="; ".join((*note_parts, segment_note)),
                    )
                )
            continue
        if record.provider in {"ccdm", "wds"} and raw_component:
            reference_component = display_reference or ("A" if display_component != "A" else "")
            if reference_component:
                anchor = record_index.get((record.source_id, record.native_id, reference_component))
                group_anchor = _wds_group_reference_position(
                    record_index,
                    record,
                    reference_component,
                ) if record.provider == "wds" else None
                if group_anchor is not None:
                    start_ra, start_dec, start_kind = group_anchor
                    if _hierarchy_geometry_usable(record):
                        link_end_ra, link_end_dec = _offset_position(
                            start_ra,
                            start_dec,
                            record.separation_arcsec,
                            record.pa_deg,
                        )
                        link_basis = "WDS reference-group midpoint plus rho/PA"
                    else:
                        link_end_ra, link_end_dec = display_ra, display_dec
                        link_basis = "WDS reference-group midpoint to catalog position"
                elif anchor is not None and anchor.ra_deg is not None and anchor.dec_deg is not None:
                    start_ra, start_dec, start_kind = _hierarchy_component_position(anchor, center)
                    link_end_ra, link_end_dec = display_ra, display_dec
                    link_basis = f"{record.provider.upper()} component positions"
                elif _hierarchy_geometry_usable(record):
                    start_ra, start_dec = record.ra_deg, record.dec_deg
                    start_kind = display_position_kind
                    link_end_ra, link_end_dec = _offset_position(
                        record.ra_deg,
                        record.dec_deg,
                        record.separation_arcsec,
                        record.pa_deg,
                    )
                    link_basis = "rho/PA endpoint"
                else:
                    start_ra = start_dec = link_end_ra = link_end_dec = None
                    start_kind = ""
                    link_basis = ""
                if start_ra is not None and start_dec is not None and link_end_ra is not None and link_end_dec is not None:
                    relation_type = _hierarchy_relation_type(
                        record.provider,
                        reference_component,
                        display_component or raw_component,
                        record.component,
                    )
                    epoch_note = (
                        "CCDM positions are plotted at epoch 2000.0"
                        if record.provider == "ccdm"
                        else "WDS link uses catalog pair geometry/position"
                    )
                    segment_note = (
                        f"{display_component or raw_component}: component link from {reference_component} ({start_kind}); "
                        f"type {relation_type}; {epoch_note}; basis {link_basis}"
                    )
                    if _hierarchy_geometry_usable(record):
                        segment_note += (
                            f"; relative measurement year {record.measure_epoch:g}"
                            if record.measure_epoch is not None else
                            "; relative measurement year unavailable"
                        )
                    else:
                        segment_note += "; no measured rho/PA in this row"
                    segments.append(
                        SkySegment(
                            kind="hierarchy_component_link",
                            provider=record.provider,
                            status=status,
                            source_id=display_source_id,
                            label=display_component or raw_component,
                            start_ra_deg=start_ra,
                            start_dec_deg=start_dec,
                            end_ra_deg=link_end_ra,
                            end_dec_deg=link_end_dec,
                            candidate_id=candidate_id,
                            target_id=candidate_target_id,
                            native_id=record.native_id,
                            reference_label=reference_component,
                            component_label=display_component or raw_component,
                            relation_type=relation_type,
                            note="; ".join((*note_parts, segment_note)),
                        )
                    )
        elif _hierarchy_geometry_usable(record):
            end_ra, end_dec = _offset_position(
                record.ra_deg,
                record.dec_deg,
                record.separation_arcsec,
                record.pa_deg,
            )
            segments.append(
                SkySegment(
                    kind="hierarchy_component_link",
                    provider=record.provider,
                    status=status,
                    source_id=display_source_id,
                    label=display_component or raw_component or record.component or record.discoverer_id or source_id,
                    start_ra_deg=record.ra_deg,
                    start_dec_deg=record.dec_deg,
                    end_ra_deg=end_ra,
                    end_dec_deg=end_dec,
                    candidate_id=candidate_id,
                    target_id=candidate_target_id,
                    native_id=record.native_id,
                    reference_label="primary",
                    component_label=display_component or raw_component or record.component or record.discoverer_id or source_id,
                    relation_type="component",
                    note="; ".join(note_parts),
                )
            )
        elif raw_reference and raw_component:
            anchor = record_index.get((record.source_id, record.native_id, display_reference or raw_reference))
            if anchor is not None and anchor.ra_deg is not None and anchor.dec_deg is not None:
                start_ra, start_dec, start_kind = _hierarchy_component_position(anchor, center)
                relation_type = _hierarchy_relation_type(
                    record.provider,
                    display_reference or raw_reference,
                    display_component or raw_component,
                    record.component,
                )
                segments.append(
                    SkySegment(
                        kind="hierarchy_component_link",
                        provider=record.provider,
                        status=status,
                        source_id=display_source_id,
                        label=display_component or raw_component,
                        start_ra_deg=start_ra,
                        start_dec_deg=start_dec,
                        end_ra_deg=display_ra,
                        end_dec_deg=display_dec,
                        candidate_id=candidate_id,
                        target_id=candidate_target_id,
                        native_id=record.native_id,
                        reference_label=display_reference or raw_reference,
                        component_label=display_component or raw_component,
                        relation_type=relation_type,
                        note="; ".join((
                            *note_parts,
                            f"{display_component or raw_component}: relative-component anchor from {display_reference or raw_reference} ({start_kind}); type {relation_type}; no measured rho/PA in this row",
                        )),
                    )
                )
    return points, segments


def _hierarchy_display_components(
    provider: str,
    raw_reference: str,
    raw_component: str,
    original_component: str | None,
) -> tuple[str, str]:
    """Return review/display reference and endpoint labels.

    WDS stores pair labels such as AB in one field. For plotting, the catalog
    coordinate is the reference/primary side and the rho/PA endpoint is the
    concerned component, so AB is best displayed as A -> B. Preserve the native
    component string in notes/attributes for provenance.
    """
    reference = raw_reference.strip()
    component = raw_component.strip()
    original = (original_component or "").strip()
    if provider == "wds":
        if "," in original:
            left, right = [part.strip() for part in original.split(",", 1)]
            if not reference:
                reference = left
            component = right or component
        compact = component.replace(" ", "")
        if not reference and len(compact) == 2 and compact.isalpha():
            return compact[0], compact[1]
    return reference, component


def _wds_blank_component_implies_ab(
    record: HierarchyRecord,
    record_index: dict[tuple[int, str, str | None], HierarchyRecord],
    raw_reference: str,
    raw_component: str,
) -> bool:
    if record.provider != "wds":
        return False
    if raw_reference.strip() or raw_component.strip() or (record.component or "").strip():
        return False
    if not _hierarchy_geometry_usable(record):
        return False
    return (record.source_id, record.native_id, "AB") not in record_index


def _hierarchy_geometry_usable(record: HierarchyRecord) -> bool:
    if record.separation_arcsec is None or record.pa_deg is None:
        return False
    if record.provider == "wds" and record.separation_arcsec >= UNUSABLE_SEPARATION_ARCSEC:
        return False
    return True


def _wds_record_has_unusable_separation(record: HierarchyRecord) -> bool:
    if record.provider != "wds":
        return False
    if record.separation_arcsec is not None and record.separation_arcsec >= UNUSABLE_SEPARATION_ARCSEC:
        return True
    return _hierarchy_raw_payload(record).get("unusable_separation_arcsec") is not None


def _hierarchy_relation_type(
    provider: str,
    reference_label: str | None,
    component_label: str | None,
    original_component: str | None,
) -> str:
    if provider != "wds":
        return "component"
    reference = (reference_label or "").strip()
    component = (component_label or "").strip()
    if _wds_same_group(reference, component):
        return "internal"
    if _wds_structural_group_reference(reference, component, original_component):
        return "group"
    return "cross_link"


def _wds_same_group(first: str, second: str) -> bool:
    first_group = _wds_component_group(first)
    second_group = _wds_component_group(second)
    return first_group is not None and first_group == second_group and (first != first_group or second != second_group)


def _wds_component_group(label: str | None) -> str | None:
    compact = (label or "").strip().replace(" ", "")
    if not compact:
        return None
    if len(compact) >= 2 and compact[0].isupper() and (compact[1].islower() or compact[1].isdigit()):
        return compact[0]
    return compact


def _wds_structural_group_reference(reference_label: str, component_label: str | None, original_component: str | None) -> bool:
    compact = (reference_label or "").strip().replace(" ", "")
    component = (component_label or "").strip().replace(" ", "")
    if not compact:
        return False
    if len(compact) > 1 and compact.isalpha() and "," in (original_component or ""):
        return True
    if len(compact) == 1 and len(component) == 1 and compact.isalpha() and component.isalpha() and compact != component:
        return True
    return False


def _hierarchy_source_id(
    record: HierarchyRecord,
    *,
    component_override: str | None = None,
) -> str:
    parts = [record.native_id]
    if component_override:
        parts.append(component_override)
    elif record.component:
        parts.append(record.component)
    elif record.discoverer_id:
        parts.append(record.discoverer_id)
    return " ".join(part for part in parts if part)


def _hierarchy_display_position(
    record: HierarchyRecord,
    center: tuple[float, float],
) -> tuple[float, float, str]:
    positions = hierarchy_record_positions(record)
    if not positions:
        raise ValueError("hierarchy record has no display position")
    if record.provider == "wds" and _hierarchy_geometry_usable(record):
        for ra_deg, dec_deg, position_kind in positions:
            if position_kind == "component endpoint":
                return ra_deg, dec_deg, "WDS PA2/Sep2 endpoint"
    return min(
        positions,
        key=lambda value: _separation_arcsec(center, value[0], value[1]),
    )


def _hierarchy_component_position(
    record: HierarchyRecord,
    center: tuple[float, float],
) -> tuple[float, float, str]:
    positions = hierarchy_record_positions(record)
    for ra_deg, dec_deg, position_kind in positions:
        if position_kind == "component endpoint":
            return ra_deg, dec_deg, position_kind
    return min(
        positions,
        key=lambda value: _separation_arcsec(center, value[0], value[1]),
    )


def _wds_group_reference_position(
    record_index: dict[tuple[int, str, str | None], HierarchyRecord],
    record: HierarchyRecord,
    reference_component: str,
) -> tuple[float, float, str] | None:
    compact = reference_component.replace(" ", "")
    if len(compact) != 2 or not compact.isalpha():
        return None
    group_record = record_index.get((record.source_id, record.native_id, compact))
    if (
        group_record is None
        or group_record.ra_deg is None
        or group_record.dec_deg is None
        or not _hierarchy_geometry_usable(group_record)
    ):
        return None
    secondary_ra, secondary_dec = _offset_position(
        group_record.ra_deg,
        group_record.dec_deg,
        group_record.separation_arcsec,
        group_record.pa_deg,
    )
    midpoint_ra, midpoint_dec = _midpoint_position(
        group_record.ra_deg,
        group_record.dec_deg,
        secondary_ra,
        secondary_dec,
    )
    return midpoint_ra, midpoint_dec, f"{compact} midpoint"


def _hierarchy_raw_payload(record: HierarchyRecord) -> dict[str, object]:
    try:
        value = json.loads(record.raw_payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _hierarchy_attribute_summaries(record: HierarchyRecord) -> tuple[str, ...]:
    values = []
    raw_payload = _hierarchy_raw_payload(record)
    if record.discoverer_id:
        values.append(f"discoverer={record.discoverer_id}")
    if record.component:
        values.append(f"component={record.component}")
    if record.separation_arcsec is not None and not _hierarchy_geometry_usable(record):
        values.append(
            f"rho={_compact_display_value(record.separation_arcsec)} arcsec unusable"
        )
    elif record.separation_arcsec is not None:
        values.append(f"rho={_compact_display_value(record.separation_arcsec)} arcsec")
    elif raw_payload.get("unusable_separation_arcsec") is not None:
        values.append(
            "rho="
            f"{_compact_display_value(float(raw_payload['unusable_separation_arcsec']))} "
            "arcsec unusable"
        )
    if record.pa_deg is not None:
        values.append(f"pa={_compact_display_value(record.pa_deg)} deg")
    if record.measure_epoch is not None:
        values.append(f"epoch={_compact_display_value(record.measure_epoch)}")
    if record.magnitude_primary is not None:
        values.append(f"mag1={_compact_display_value(record.magnitude_primary)}")
    if record.magnitude_secondary is not None:
        values.append(f"mag2={_compact_display_value(record.magnitude_secondary)}")
    return tuple(values)


def _display_position_2000(
    ra_deg: float,
    dec_deg: float,
    epoch: float,
    solution: AstrometricSolution | Astrometry | None,
    *,
    native_pm: tuple[float, float, str] | None = None,
    base_note: str,
) -> tuple[float, float, float | None, float | None, str | None, str]:
    pm_ra = None
    pm_dec = None
    pm_source = None
    if native_pm is not None:
        pm_ra, pm_dec, pm_source = native_pm
    elif (
        solution is not None
        and solution.proper_motion_available
        and solution.pm_ra_cosdec_masyr is not None
        and solution.pm_dec_masyr is not None
    ):
        pm_ra = solution.pm_ra_cosdec_masyr
        pm_dec = solution.pm_dec_masyr
        pm_source = f"assumed target PM ({solution.source})"
    if math.isclose(epoch, 2000.0):
        return ra_deg, dec_deg, pm_ra, pm_dec, pm_source, f"{base_note}; plotted at epoch 2000.0"
    if pm_ra is None or pm_dec is None:
        return ra_deg, dec_deg, None, None, None, (
            f"{base_note}; native epoch {epoch:g}, plotted without propagation because no target PM is available"
        )
    propagated = propagate_to_epoch(
        Astrometry(
            ra_deg,
            dec_deg,
            epoch,
            pm_ra_cosdec_masyr=pm_ra,
            pm_dec_masyr=pm_dec,
            source="review",
        ),
        2000.0,
    )
    qualifier = (
        "using native source PM"
        if native_pm is not None
        else "using target PM as counterpart hypothesis"
    )
    return propagated.ra_deg, propagated.dec_deg, pm_ra, pm_dec, pm_source, (
        f"{base_note}; native epoch {epoch:g}, plotted at epoch 2000.0 {qualifier}"
    )


def _measurement_summaries(session: Session, raw_row_id: int, *, limit: int = 8) -> tuple[str, ...]:
    raw = session.get(RawCatalogRow, raw_row_id)
    if raw is None:
        return ()
    rows = session.scalars(
        select(NormalizedMeasurement)
        .where(NormalizedMeasurement.detection_id == raw.detection_id)
        .order_by(NormalizedMeasurement.band)
    )
    summaries = []
    for measurement in rows:
        marker = "<" if measurement.upper_limit else ""
        error = (
            f" ± {_compact_display_value(measurement.error)}"
            if measurement.error else ""
        )
        flags = []
        if measurement.quality:
            flags.append(str(measurement.quality))
        if measurement.excluded:
            flags.append("excluded")
        if measurement.blend_state != "clear":
            flags.append(measurement.blend_state)
        suffix = f" ({', '.join(flags)})" if flags else ""
        summaries.append(
            f"{measurement.band}={marker}{_compact_display_value(measurement.value)}"
            f"{error} {measurement.unit}{suffix}"
        )
        if len(summaries) >= limit:
            break
    return tuple(summaries)


def _measurement_beams(session: Session, raw_row_id: int, *, limit: int = 12) -> tuple[PhotometryBeam, ...]:
    raw = session.get(RawCatalogRow, raw_row_id)
    if raw is None:
        return ()
    rows = session.scalars(
        select(NormalizedMeasurement)
        .where(NormalizedMeasurement.detection_id == raw.detection_id)
        .where(NormalizedMeasurement.resolution_major_arcsec.is_not(None))
        .order_by(
            NormalizedMeasurement.provider,
            NormalizedMeasurement.resolution_major_arcsec,
            NormalizedMeasurement.band,
        )
    )
    beams = []
    for measurement in rows:
        if measurement.resolution_major_arcsec is None:
            continue
        beams.append(
            PhotometryBeam(
                provider=measurement.provider,
                band=measurement.band,
                major_arcsec=measurement.resolution_major_arcsec,
                minor_arcsec=measurement.resolution_minor_arcsec,
                kind=measurement.resolution_kind,
                reference=measurement.resolution_reference,
                ownership_scope=measurement.ownership_scope,
                blend_state=measurement.blend_state,
                blend_reason=measurement.blend_reason,
                value=measurement.value,
                error=measurement.error,
                unit=measurement.unit,
                upper_limit=measurement.upper_limit,
            )
        )
        if len(beams) >= limit:
            break
    return tuple(beams)


def _catalog_payload(payload_json: str) -> dict[str, object] | None:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _catalog_association(payload_json: str) -> dict[str, object]:
    payload = _catalog_payload(payload_json)
    if payload is None:
        return {}
    association = payload.get("_sdb_association")
    return association if isinstance(association, dict) else {}


def _compact_display_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            absolute = abs(number)
            if absolute == 0 or absolute >= 0.01:
                return f"{number:.2f}"
            decimals = min(10, max(3, math.ceil(-math.log10(absolute)) + 1))
            return f"{number:.{decimals}f}"
    return str(value)


def _catalog_payload_summaries(
    provider: str, payload_json: str, *, limit: int = 8
) -> tuple[str, ...]:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    payload = normalize_review_payload(provider, payload)
    values: list[str] = []
    association = payload.get("_sdb_association")
    if isinstance(association, dict):
        if association.get("review_only"):
            values.append("review-only catalogue neighbour")
        candidate_separation = association.get("candidate_separation_arcsec")
        acceptance_radius = association.get("acceptance_radius_arcsec")
        query_radius = association.get("query_radius_arcsec")
        if candidate_separation is not None:
            values.append(
                f"candidate separation={_compact_display_value(candidate_separation)} arcsec"
            )
        if acceptance_radius is not None:
            values.append(
                f"acceptance radius={_compact_display_value(acceptance_radius)} arcsec"
            )
        if query_radius is not None:
            values.append(
                f"query radius={_compact_display_value(query_radius)} arcsec"
            )
        if association.get("identifier_agreement"):
            values.append("identifier agrees with target aliases")
    review = payload.get("_sdb_review")
    fields = review.get("fields", ()) if isinstance(review, dict) else ()
    for field in fields if isinstance(fields, list) else ():
        if not isinstance(field, dict):
            continue
        value = field.get("value")
        if value is None or str(value).strip() == "":
            continue
        label = field.get("label") or field.get("key") or "catalog attribute"
        source = field.get("source_column")
        unit = field.get("unit")
        source_text = f" ({source})" if source else ""
        unit_text = f" {unit}" if unit else ""
        values.append(
            f"{label}{source_text}={_compact_display_value(value)}{unit_text}"
        )
        if len(values) >= limit:
            break
    return tuple(values)




def _attribute_summaries(session: Session, raw_row_id: int, *, limit: int = 8) -> tuple[str, ...]:
    rows = session.scalars(
        select(CatalogAttribute)
        .where(CatalogAttribute.raw_row_id == raw_row_id)
        .order_by(CatalogAttribute.key)
    )
    summaries = []
    for attribute in rows:
        value = attribute.value_text if attribute.value_text is not None else attribute.value_float
        if value is None:
            continue
        error = (
            f" ± {_compact_display_value(attribute.uncertainty)}"
            if attribute.uncertainty is not None else ""
        )
        unit = f" {attribute.unit}" if attribute.unit else ""
        quality = f" [{attribute.quality}]" if attribute.quality else ""
        summaries.append(
            f"{attribute.key}={_compact_display_value(value)}{error}{unit}{quality}"
        )
        if len(summaries) >= limit:
            break
    return tuple(summaries)


def _attribute_pm(
    session: Session, raw_row_id: int, *, provider: str
) -> tuple[float, float, str] | None:
    # Older AllWISE imports used these generic keys for the catalog's
    # short-baseline apparent-motion fit. Never reinterpret those historical
    # attributes as stellar proper motion; refreshed rows use explicit
    # ``apparent_motion_*`` keys instead.
    if provider == "allwise":
        return None
    rows = list(session.scalars(
        select(CatalogAttribute).where(CatalogAttribute.raw_row_id == raw_row_id)
    ))
    values = {row.key: row for row in rows}
    pm_ra = values.get("pm_ra_cosdec")
    pm_dec = values.get("pm_dec")
    if pm_ra is None or pm_dec is None or pm_ra.value_float is None or pm_dec.value_float is None:
        return None
    source = pm_ra.reference or pm_dec.reference or "native catalog PM"
    return pm_ra.value_float, pm_dec.value_float, source


def _position_uncertainty_arcsec(
    provider: str, payload_json: str
) -> tuple[float | None, float | None]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    payload = normalize_review_payload(provider, payload)
    review = payload.get("_sdb_review")
    uncertainty = review.get("position_uncertainty") if isinstance(review, dict) else None
    if not isinstance(uncertainty, dict):
        return None, None
    try:
        major = float(uncertainty["major_arcsec"])
        minor = float(uncertainty["minor_arcsec"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not all(math.isfinite(value) and value > 0 for value in (major, minor)):
        return None, None
    return major, minor


def _simbad_metadata_points(session: Session, target: Target, center: tuple[float, float]) -> list[SkyPoint]:
    rows = session.scalars(
        select(SimbadMetadata)
        .where(SimbadMetadata.target_id == target.id)
        .order_by(SimbadMetadata.id.desc())
        .limit(1)
    )
    solution = _target_solution(session, target)
    use_solution_pm = (
        solution is not None
        and solution.source == "simbad"
        and solution.proper_motion_available
        and solution.pm_ra_cosdec_masyr is not None
        and solution.pm_dec_masyr is not None
    )
    points = []
    for row in rows:
        note = "current/latest SIMBAD metadata position"
        pm_ra = None
        pm_dec = None
        pm_source = None
        if row.pm_ra_cosdec_masyr is not None and row.pm_dec_masyr is not None:
            pm_ra = row.pm_ra_cosdec_masyr
            pm_dec = row.pm_dec_masyr
            pm_source = row.proper_motion_bibcode or "simbad metadata"
            note += "; PM from SIMBAD metadata"
        elif use_solution_pm:
            pm_ra = solution.pm_ra_cosdec_masyr
            pm_dec = solution.pm_dec_masyr
            pm_source = "canonical simbad astrometry"
            note += "; PM from canonical SIMBAD astrometric solution"
        points.append(
            SkyPoint(
                kind="metadata",
                provider="simbad",
                status="match",
                source_id=row.main_id,
                ra_deg=row.ra_deg,
                dec_deg=row.dec_deg,
                separation_arcsec=_separation_arcsec(center, row.ra_deg, row.dec_deg),
                accepted=True,
                run_id=row.run_id,
                pm_ra_cosdec_masyr=pm_ra,
                pm_dec_masyr=pm_dec,
                pm_source=pm_source,
                note=note,
            )
        )
    return points


def _separation_arcsec(center: tuple[float, float], ra_deg: float, dec_deg: float) -> float:
    x, y = _offset_arcsec(center, ra_deg, dec_deg)
    return math.hypot(x, y)


def _segment_farthest_offset(center: tuple[float, float], segment: SkySegment) -> float:
    start = _offset_arcsec(center, segment.start_ra_deg, segment.start_dec_deg)
    end = _offset_arcsec(center, segment.end_ra_deg, segment.end_dec_deg)
    return max(math.hypot(*start), math.hypot(*end))


def _offset_position(
    ra_deg: float,
    dec_deg: float,
    separation_arcsec: float,
    pa_deg: float,
) -> tuple[float, float]:
    pa = math.radians(pa_deg)
    east_arcsec = separation_arcsec * math.sin(pa)
    north_arcsec = separation_arcsec * math.cos(pa)
    cos_dec = max(0.01, abs(math.cos(math.radians(dec_deg))))
    return (
        (ra_deg + east_arcsec / (3600.0 * cos_dec)) % 360.0,
        dec_deg + north_arcsec / 3600.0,
    )


def _midpoint_position(
    first_ra_deg: float,
    first_dec_deg: float,
    second_ra_deg: float,
    second_dec_deg: float,
) -> tuple[float, float]:
    dra = second_ra_deg - first_ra_deg
    if dra > 180.0:
        dra -= 360.0
    elif dra < -180.0:
        dra += 360.0
    return (
        (first_ra_deg + dra / 2.0) % 360.0,
        (first_dec_deg + second_dec_deg) / 2.0,
    )


def _offset_arcsec(center: tuple[float, float], ra_deg: float, dec_deg: float) -> tuple[float, float]:
    ra0, dec0 = center
    dra = ra_deg - ra0
    if dra > 180.0:
        dra -= 360.0
    elif dra < -180.0:
        dra += 360.0
    x = dra * math.cos(math.radians(dec0)) * 3600.0
    y = (dec_deg - dec0) * 3600.0
    return x, y
