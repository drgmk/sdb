"""Plotly and HTML rendering for the transport-independent sky projection."""

from __future__ import annotations

import html
import json
import math
from dataclasses import asdict
from pathlib import Path

from ..catalogs.provenance import vizier_entry_url
from .sky_view import ReviewSkyView


_HIERARCHY_VIZIER_LOCATORS = {
    "wds": ("B/wds/wds", "WDS"),
    "ccdm": ("I/274/ccdm", "CCDM"),
}


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


def _compact_display_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            absolute = abs(number)
            if absolute == 0 or absolute >= 0.01:
                return f"{number:.2f}"
            decimals = min(
                10,
                max(3, math.ceil(-math.log10(absolute)) + 1),
            )
            return f"{number:.{decimals}f}"
    return str(value)


def _offset_arcsec(
    center: tuple[float, float],
    ra_deg: float,
    dec_deg: float,
) -> tuple[float, float]:
    ra0, dec0 = center
    dra = ra_deg - ra0
    if dra > 180.0:
        dra -= 360.0
    elif dra < -180.0:
        dra += 360.0
    return (
        dra * math.cos(math.radians(dec0)) * 3600.0,
        (dec_deg - dec0) * 3600.0,
    )

