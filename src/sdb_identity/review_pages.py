"""HTML rendering for the local review interface."""

from __future__ import annotations

import html
import json
import math
import os
from collections import defaultdict
from importlib.resources import files
from string import Template
from urllib.parse import quote, urlencode

from .review_workspace import (
    TargetWorkspace,
    filtered_queue_rows,
    queue_query,
)


def _review_asset(name: str) -> str:
    return files("sdb_identity.review_assets").joinpath(name).read_text(
        encoding="utf-8"
    )


_CSS = _review_asset("review.css")
_WORKSPACE_JS = _review_asset("workspace.js")


def _template(name: str, **values: object) -> str:
    return Template(_review_asset(name)).substitute(
        {key: str(value) for key, value in values.items()}
    )


def _select_options(
    values: list[str], selected: str, *, empty_label: str,
) -> str:
    options = [
        f"<option value=''{' selected' if not selected else ''}>{_e(empty_label)}</option>"
    ]
    options.extend(
        f"<option value='{_e(value)}'{' selected' if value == selected else ''}>"
        f"{_e(value.replace('_', ' '))}</option>"
        for value in values
    )
    return "".join(options)


def render_queue_page(
    sample: str,
    report: dict[str, object],
    filters: dict[str, str],
) -> str:
    filtered_rows = filtered_queue_rows(report, filters)
    rows = []
    for position, row in enumerate(filtered_rows):
        provider_text = ", ".join(
            str(value["provider"]) for value in row["providers"]
        )
        target_query = queue_query(filters, position)
        target_url = (
            f"/target/{quote(str(row['sdbid']))}{_e(target_query)}"
        )
        display_name_html = (
            f"<a href='{target_url}'>{_e(row['display_name'])}</a>"
            if row.get("display_name") else "<span class='muted'>—</span>"
        )
        rows.append(
            f"<tr class='priority-{_e(row['priority'])}' data-classification='{_e(row['classification'])}'>"
            f"<td>{_e(row['priority'])}</td>"
            f"<td>{display_name_html}</td>"
            f"<td><a href='{target_url}'><code>{_e(row['sdbid'])}</code></a></td>"
            f"<td>{_e(str(row['classification']).replace('_', ' '))}</td>"
            f"<td>{_e(row['role'])}</td><td>{row['detection_count']} / {row['measurement_count']} bands</td>"
            f"<td>{row['unassigned_detection_count']} / {row['mixed_detection_count']}</td>"
            f"<td>{_e(provider_text)}</td>"
            f"<td>{_e(row['recommended_action'])}</td></tr>"
        )
    summary = report["summary"]
    all_rows = list(report["rows"])
    available_priorities = {str(row["priority"]) for row in all_rows}
    priorities = [
        value for value in ("highest", "high", "medium", "low")
        if value in available_priorities
    ] + sorted(available_priorities - {"highest", "high", "medium", "low"})
    roles = sorted({str(row["role"]) for row in all_rows})
    classifications = sorted({str(row["classification"]) for row in all_rows})
    providers = sorted({
        str(value["provider"]) for row in all_rows for value in row["providers"]
    })
    body = _template(
        "queue.html",
        sample=_e(sample),
        target_count=summary["target_count"],
        actionable_target_count=summary["actionable_target_count"],
        clean_target_count=summary["clean_target_count"],
        scope_blocker_target_count=summary["scope_blocker_target_count"],
        unassigned_detection_count=summary["unassigned_detection_count"],
        mixed_detection_count=summary["mixed_detection_count"],
        view_options=_select_options(
            ["all", "clean"],
            ""
            if filters.get("view", "actionable") == "actionable"
            else filters.get("view", ""),
            empty_label="actionable",
        ),
        priority_options=_select_options(
            priorities,
            filters.get("priority", ""),
            empty_label="all priorities",
        ),
        role_options=_select_options(
            roles, filters.get("role", ""), empty_label="all roles"
        ),
        classification_options=_select_options(
            classifications,
            filters.get("classification", ""),
            empty_label="all classifications",
        ),
        provider_options=_select_options(
            providers,
            filters.get("provider", ""),
            empty_label="all providers",
        ),
        search=_e(filters.get("search", "")),
        shown_count=len(filtered_rows),
        total_count=len(all_rows),
        rows=(
            "".join(rows)
            or '<tr><td colspan="9" class="muted">'
            "No sample targets match these filters.</td></tr>"
        ),
    )
    return render_page(f"SDB review: {sample}", body)


def render_catalogs_page(report: dict[str, object]) -> str:
    rows = []
    for provider in report["providers"]:
        bands = ", ".join(
            f"{band['name']} ({band['wavelength_micron']:g} µm)"
            for band in provider["bands"]
        ) or "—"
        science_tables = ", ".join(provider["science_tables"]) or "—"
        retained_tables = ", ".join(provider["retained_tables"]) or "—"
        snapshot = provider.get("snapshot")
        snapshot_detail = ""
        if snapshot:
            table_rows = "".join(
                f"<li><code>{_e(table['name'])}</code>: "
                f"{table['row_count']:,} rows"
                f"{' (science)' if table['science'] else ' (retained only)'}</li>"
                for table in snapshot["tables"]
            )
            snapshot_detail = (
                f"<p><strong>Snapshot:</strong> {snapshot['row_count']:,} rows; "
                f"retrieved {_e(snapshot['retrieved_at'])}; "
                f"SHA-256 <code>{_e(snapshot['content_sha256'])}</code></p>"
                f"<ul>{table_rows}</ul>"
            )
        caveats = "".join(
            f"<li>{_e(value)}</li>" for value in provider["caveats"]
        )
        details = f"""
<div class="catalog-detail">
  <p><strong>Science tables:</strong> {_e(science_tables)}<br>
  <strong>Retained-only tables:</strong> {_e(retained_tables)}<br>
  <strong>Identifier policy:</strong> {_e(provider['identifier_policy'])}<br>
  <strong>Component policy:</strong> {_e(provider['component_policy'])}<br>
  <strong>Epoch:</strong> {_e(provider['query_epoch'] if provider['query_epoch'] is not None else 'source identifier')} ·
  <strong>query radius:</strong> {_e(str(provider['radius_arcsec']) + ' arcsec' if provider['radius_arcsec'] is not None else 'n/a')} ·
  <strong>review radius:</strong> {_e(str(provider['review_radius_arcsec']) + ' arcsec' if provider['review_radius_arcsec'] is not None else 'n/a')}<br>
  <strong>Bibliography:</strong> <code>{_e(provider['bibliography'] or '—')}</code></p>
  {snapshot_detail}
  {f'<ul class="warning-list">{caveats}</ul>' if caveats else ''}
</div>"""
        rows.append(
            f"<tr><td><details><summary><strong>{_e(provider['display_name'])}</strong> "
            f"<code>{_e(provider['key'])}</code></summary>{details}</details></td>"
            f"<td><a href='{_e(provider['vizier_url'])}' target='_blank' rel='noopener'>"
            f"<code>{_e(provider['catalog'])}</code></a></td>"
            f"<td>{_e(str(provider['acquisition_mode']).replace('_', ' '))}</td>"
            f"<td>{_e(bands)}</td><td class='catalog-status-{_e(provider['status'])}'>"
            f"{_e(provider['status'])}</td></tr>"
        )
    body = _template(
        "catalogs.html",
        provider_count=report["provider_count"],
        remote_count=report["remote_count"],
        snapshot_current_count=report["snapshot_current_count"],
        snapshot_missing_count=report["snapshot_missing_count"],
        rows="".join(rows),
    )
    return render_page("SDB catalog providers", body)


def _target_external_resources(
    identifier: str,
    *,
    ra_deg: object,
    dec_deg: object,
) -> list[dict[str, str]]:
    resources = [{
        "label": "SIMBAD",
        "title": "SIMBAD",
        "url": (
            "https://simbad.cds.unistra.fr/simbad/sim-id?"
            + urlencode({
                "submit": "submit id",
                "Ident": identifier,
            })
        ),
    }]
    if ra_deg is None or dec_deg is None:
        return resources
    ra = str(float(ra_deg))
    dec = str(float(dec_deg))
    coordinate = f"{ra} {dec}"
    comma_coordinate = f"{ra},{dec}"
    resources.extend([
        {
            "label": "CDS",
            "title": "CDS Portal",
            "url": (
                "https://cdsportal.u-strasbg.fr/?"
                + urlencode({"target": coordinate})
            ),
        },
        {
            "label": "CASSIS",
            "title": "Cornell Atlas of Spitzer IRS Sources",
            "url": (
                "https://cassis.sirtf.com/atlas/cgi/radec.py?"
                + urlencode({"ra": ra, "dec": dec, "radius": 20})
            ),
        },
        {
            "label": "Finder",
            "title": "IRSA Finder Chart",
            "url": (
                "https://irsa.ipac.caltech.edu/applications/finderchart/"
                "servlet/api?"
                + urlencode({
                    "mode": "getResult",
                    "locstr": comma_coordinate,
                })
            ),
        },
        {
            "label": "Spitzer",
            "title": "Spitzer Heritage Archive",
            "url": (
                "https://sha.ipac.caltech.edu/applications/Spitzer/SHA/?"
                + urlencode({
                    "api": "search",
                    "searchoption": "POSITION",
                    "sr": "180s",
                    "WorldPt": f"{ra};{dec};EQ_J2000",
                    "execute": "true",
                })
            ),
        },
        {
            "label": "MAST",
            "title": "MAST Portal",
            "url": (
                "https://mast.stsci.edu/portal/Mashup/Clients/Mast/"
                "Portal.html?"
                + urlencode({"searchQuery": comma_coordinate})
            ),
        },
        {
            "label": "ESASky",
            "title": "ESASky",
            "url": (
                "https://sky.esa.int/?"
                + urlencode({
                    "action": "goto",
                    "fov": "0.25",
                    "cooframe": "J2000",
                    "sci": "true",
                    "hips": "AllWISE color",
                    "target": coordinate,
                })
            ),
        },
    ])
    return resources


def render_target_page(workspace: TargetWorkspace) -> str:
    sdbid = workspace.sdbid
    readiness = workspace.readiness
    graph = workspace.fitting_graph
    raw_row_detections = workspace.raw_row_detections
    navigation = workspace.navigation
    display_name = workspace.display_name
    simbad_main_ids = workspace.simbad_main_ids
    catalog_coverage = list(workspace.catalog_coverage)
    catalog_update_available = workspace.catalog_update_available
    nearby_import_available = workspace.nearby_import_available
    target_position = workspace.target_position
    default_actor = os.environ.get("SDB_ACTOR", "").strip()
    target = next(
        (row for row in graph["targets"] if row["sdbid"] == sdbid),
        None,
    )
    if target is None:
        raise KeyError(f"target is not present in its fitting graph: {sdbid}")
    targets = sorted(graph["targets"], key=lambda row: str(row["sdbid"]))

    def target_label(row: dict[str, object]) -> str:
        target_sdbid = str(row["sdbid"])
        return simbad_main_ids.get(target_sdbid, target_sdbid)

    def source_html(
        label: object,
        rows: list[dict[str, object]],
    ) -> str:
        for row in rows:
            access_url = str(row.get("access_url") or "")
            if access_url.startswith("https://"):
                return (
                    f"<a href='{_e(access_url)}' target='_blank' "
                    f"rel='noopener'>{_e(label)}</a>"
                )
        return _e(label)

    requested_target_label = simbad_main_ids.get(sdbid, display_name or sdbid)
    target_position = target_position or {}
    external_resources = _target_external_resources(
        requested_target_label,
        ra_deg=target_position.get("ra2000_deg"),
        dec_deg=target_position.get("dec2000_deg"),
    )
    external_resource_html = "".join(
        f"<a class='external-resource' href='{_e(row['url'])}' target='_blank' "
        f"rel='noopener' title='{_e(row['title'])}'>{_e(row['label'])}</a>"
        for row in external_resources
    )
    catalog_coverage = catalog_coverage or []
    coverage_missing = sum(
        len(row["missing_providers"]) for row in catalog_coverage
    )
    coverage_total = sum(
        int(row["expected_count"]) for row in catalog_coverage
    )
    coverage_current = coverage_total - coverage_missing
    coverage_normalization = len({
        int(gap["detection_id"])
        for row in catalog_coverage
        for gap in row.get("normalization_gaps", [])
    })
    coverage_label = (
        f"Catalog coverage {coverage_current}/{coverage_total}"
        if coverage_total
        else "Catalog coverage"
    )
    if coverage_normalization:
        coverage_label += f" · {coverage_normalization} to normalize"
    detection_rows: dict[int, list[dict[str, object]]] = defaultdict(list)
    for measurement in graph["measurements"]:
        detection_rows[int(measurement["detection_id"])].append(measurement)
    cards = []
    for detection_id, measurements in sorted(
        detection_rows.items(),
        key=lambda item: (item[1][0]["provider"], item[1][0]["source_id"]),
    ):
        first = measurements[0]
        source = source_html(
            first.get("source_display_name") or first["source_id"],
            list(first.get("provenance") or []),
        )
        current_contributors = set.intersection(*(
            set(row["contributor_sdbids"]) for row in measurements
        ))
        all_current_contributors = set().union(*(
            set(row["contributor_sdbids"]) for row in measurements
        ))
        current_scopes = set.intersection(*(
            set(row["composite_scope_sdbids"]) for row in measurements
        ))
        all_current_scopes = set().union(*(
            set(row["composite_scope_sdbids"]) for row in measurements
        ))
        contributor_patterns = {
            tuple(sorted(row["contributor_sdbids"])) for row in measurements
        }
        scope_patterns = {
            tuple(sorted(row["composite_scope_sdbids"])) for row in measurements
        }
        mixed_assignments = len(contributor_patterns) > 1 or len(scope_patterns) > 1
        ordinary_default = (
            not mixed_assignments
            and len(current_contributors) == 1
            and not current_scopes
            and all(
                len(row["assignments"]) == 1
                and row["assignments"][0].get("derived") is True
                and row["assignments"][0]["role"] == "contributor"
                for row in measurements
            )
        )
        target_choices = "".join(
            f"<label><input type='checkbox' class='contributor' "
            f"value='{_e(row['sdbid'])}'"
            f"{' checked' if row['sdbid'] in current_contributors else ''}> "
            f"<code>{_e(target_label(row))}</code> ({_e(row['role'])})</label>"
            for row in targets
            if row["model_target"] or row["sdbid"] in all_current_contributors
        )
        default_scope = next(iter(sorted(current_scopes)), None)
        if default_scope is None:
            default_scope = (
                sdbid if sdbid in all_current_scopes
                else str(first["origin_sdbid"] or sdbid)
            )
        has_combined_system = len(targets) > 1
        scope_choices = "".join(
            f"<option value='{_e(row['sdbid'])}'"
            f"{' selected' if row['sdbid'] == default_scope else ''}>"
            f"{_e(target_label(row))} ({_e(row['role'])})</option>"
            for row in targets
        )
        combined_system_control = (
            "<div class='combined-system-control'>"
            "<label><input type='checkbox' class='composite-scope'"
            f"{' checked' if default_scope in current_scopes else ''}> "
            "Measurement applies to the combined system</label>"
            f"<label class='scope-target-field'"
            f"{'' if default_scope in current_scopes else ' hidden'}>"
            f"System target <select class='scope-target'>{scope_choices}</select></label>"
            "</div>"
            if has_combined_system
            else ""
        )
        band_rows = []
        for row in measurements:
            excluded = bool(row["fit_excluded"])
            basis = str(row["exclusion_basis"] or "")
            status = "Excluded from fit" if excluded else "Included in fit"
            basis_label = {
                "provider_excluded": "provider default",
                "manual_exclude_action": "manual decision",
                "manual_include_action": "manual decision",
                "shared_detection": "shared-source safety",
                "iras_alternate": "IRAS duplicate safety",
                "tdsc_preferred": "TDSC preferred",
            }.get(basis, "")
            if basis_label:
                status += f" · {basis_label}"
            band_rows.append(
                f"<div class='band-row'><label><input type='checkbox' "
                f"class='measurement' value='{row['measurement_id']}' checked> "
                f"{_e(row['band'])}: {_display_number(row['value'])} ± "
                f"{_display_number(row['error'])} {_e(row['unit'])}</label>"
                f"<span class='eligibility-state {'excluded' if excluded else 'included'}'"
                f" data-current-label='{_e(status)}'>{_e(status)}</span>"
                f"<button type='button' class='eligibility-toggle' "
                f"data-current-excluded='{str(excluded).lower()}' "
                f"data-desired-excluded='{str(excluded).lower()}' "
                f"data-measurement='{row['measurement_id']}' "
                f"aria-pressed='false'>"
                f"{'Include in fit' if excluded else 'Exclude from fit'}</button></div>"
            )
        bands = "".join(band_rows)
        contributor_editor = (
            f"<h4>Contributors</h4><div class='choices'>{target_choices}</div>"
            f"{combined_system_control}"
        )
        if ordinary_default:
            default_sdbid = next(iter(current_contributors))
            default_target = next(
                (row for row in targets if row["sdbid"] == default_sdbid),
                None,
            )
            default_label = (
                default_sdbid
                if default_target is None
                else target_label(default_target)
            )
            attribution = (
                "<p class='assignment-default'>Photometry follows the accepted "
                f"source association to <code>{_e(default_label)}</code>. "
                "No separate assignment decision is needed.</p>"
                "<details class='attribution-exception'><summary>Change attribution"
                " (exception)</summary>"
                f"{contributor_editor}"
                "<button class='preview'>Preview attribution change</button>"
                "</details>"
                "<div class='drawer-actions'><button class='preview-eligibility' "
                "type='button'>Preview include/exclude</button></div>"
            )
        else:
            attribution = (
                contributor_editor
                + "<div class='drawer-actions'><button class='preview'>"
                "Preview decision</button><button class='preview-eligibility' "
                "type='button'>Preview include/exclude</button></div>"
            )
        cards.append(f"""
<section class="detection" data-detection="{detection_id}">
  <h3>{_e(first['provider'])} · {source}</h3>
  <div class="bands">{bands}</div>
  {"<p class='warning'>Bands currently have different assignments. Their common assignments are selected below; preview carefully before applying.</p>" if mixed_assignments else ""}
  {attribution}
</section>""")
    readiness_text = (
        "No system-level blocker for this target."
        if not readiness["rows"]
        else _e(readiness["rows"][0]["recommended_action"])
    )
    if navigation is None:
        back_url = "/"
        navigation_html = ""
    else:
        back_url = str(navigation["back_url"])
        previous = (
            "<span class='nav-disabled'>← Previous</span>"
            if navigation["previous_url"] is None
            else f"<a rel='prev' href='{_e(navigation['previous_url'])}'>← Previous</a>"
        )
        following = (
            "<span class='nav-disabled'>Next →</span>"
            if navigation["next_url"] is None
            else f"<a rel='next' href='{_e(navigation['next_url'])}'>Next →</a>"
        )
        queue_state = (
            f"{navigation['position']} of {navigation['count']}"
            if navigation["current_present"]
            else f"resolved/filtered out · {navigation['count']} remain"
        )
        navigation_html = (
            f"<nav class='queue-navigation' aria-label='Readiness queue navigation'>"
            f"{previous}<span>{_e(queue_state)}</span>{following}</nav>"
        )
    body = _template(
        "target.html",
        back_url=_e(back_url),
        target_heading=(
            (f"{_e(display_name)} · " if display_name else "")
            + f"<code>{_e(sdbid)}</code>"
        ),
        target_role=_e(target["role"]),
        target_state=_e(target["state"]),
        readiness_text=readiness_text,
        navigation=navigation_html,
        external_resources=external_resource_html,
        nearby_import_disabled="" if nearby_import_available else " disabled",
        nearby_import_title=_e(
            "Search SIMBAD around this target and import selected objects"
            if nearby_import_available
            else "Nearby import is unavailable in offline review mode"
        ),
        coverage_class="needs-attention" if coverage_missing else "",
        coverage_label=_e(coverage_label),
        role_button_class=(
            "needs-decision" if target["role"] == "unspecified" else ""
        ),
        role_button_label=(
            "Decide target role"
            if target["role"] == "unspecified"
            else "Change target role"
        ),
        sdbid=_e(sdbid),
        quoted_sdbid=quote(sdbid),
        default_actor=_e(default_actor),
        detection_cards="".join(cards) or "<p>No current measurements.</p>",
        requested_target_label=_e(requested_target_label),
        physical_checked=(
            " checked" if target["role"] != "composite" else ""
        ),
        composite_checked=(
            " checked" if target["role"] == "composite" else ""
        ),
        sdbid_json=json.dumps(sdbid),
        target_names_json=json.dumps(simbad_main_ids, sort_keys=True),
        raw_row_detections_json=json.dumps(
            raw_row_detections, sort_keys=True
        ),
        catalog_update_available_json=json.dumps(catalog_update_available),
        workspace_js=_WORKSPACE_JS,
    )
    return render_page(
        f"SDB review: {sdbid}", body, body_class="live-review"
    )


def render_page(title: str, body: str, *, body_class: str = "") -> str:
    return _template(
        "page.html",
        title=_e(title),
        css=_CSS,
        body_class=_e(body_class),
        body=body,
    )



def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _display_number(value: object) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(value)
    absolute = abs(number)
    if absolute == 0 or absolute >= 0.01:
        return f"{number:.2f}"
    decimals = min(10, max(3, math.ceil(-math.log10(absolute)) + 1))
    return f"{number:.{decimals}f}"
