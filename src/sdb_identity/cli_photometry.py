"""Photometry CLI registration and internally separated command handlers."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
import os
from pathlib import Path
import sys

from sqlalchemy import select

from .cli_context import CliContext
from .models.identity import Target
from .vocabulary import MeasurementTargetRole, review_priority_rank


def register_photometry_parser(
    commands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    photometry = add_parser(
        commands,
        "photometry",
        "Review and override normalized photometry inclusion.",
        "Use this to exclude or re-include specific band/provider measurements "
        "while preserving the raw provider row. Overrides are audited and affect "
        "future exports.",
    )
    subcommands = photometry.add_subparsers(
        dest="photometry_command",
        required=True,
    )
    _register_eligibility_commands(
        subcommands, add_parser, add_actor_argument, add_reason_argument,
    )
    _register_review_commands(subcommands, add_parser)
    _register_assignment_commands(
        subcommands, add_parser, add_actor_argument, add_reason_argument,
    )
    _register_proposal_commands(subcommands, add_parser)
    _register_fitting_commands(subcommands, add_parser)


def _register_eligibility_commands(
    subcommands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    for action in ("exclude", "include"):
        command = add_parser(
            subcommands,
            action,
            f"{action.title()} one normalized photometry measurement.",
            "This records an audited action for one canonical measurement without "
            "deleting provider data. Excluded measurements remain inspectable and "
            "can be included again later.",
        )
        command.add_argument("measurement_id", type=int)
        add_actor_argument(command)
        add_reason_argument(command)
    overrides = add_parser(
        subcommands,
        "overrides",
        "List photometry inclusion/exclusion actions for a target.",
        "Shows the append-only include/exclude decisions recorded for a target's "
        "measurements, with actor and reason. It does not list the measurements "
        "themselves; use `photometry review` for association context.",
    )
    overrides.add_argument("target")


def _register_review_commands(subcommands, add_parser) -> None:
    review = add_parser(
        subcommands,
        "review",
        "Review current photometry association context for a target.",
        "Lists current normalized measurements plus unaccepted current raw catalog "
        "rows. This is read-only and intended to precede assign (ownership) and "
        "include/exclude (fit eligibility) decisions.",
    )
    review.add_argument("target")
    queue = add_parser(
        subcommands,
        "review-queue",
        "Prioritize photometry association rows needing review.",
        "Combines current photometry, unaccepted catalog neighbours, and "
        "hierarchy/resolution predictions. Select exactly one target set with "
        "TARGET, --sample, or --all.",
    )
    queue.add_argument("target", nargs="?")
    queue.add_argument("--sample")
    queue.add_argument("--all", action="store_true", dest="review_all")
    queue.add_argument("--provider")
    queue.add_argument("--format", choices=["table", "json", "jsonl"], default="table")
    html = add_parser(
        subcommands,
        "review-html",
        "Write a static photometry review HTML bundle.",
        "Generates one interactive review page for every selected target and an "
        "index page summarizing photometry review-queue signals. This is static "
        "output suitable for sample review or linking from exported products.",
    )
    html.add_argument("target", nargs="?")
    html.add_argument("--sample")
    html.add_argument("--all", action="store_true", dest="review_all")
    html.add_argument("--provider")
    html.add_argument("--output-dir", required=True)
    html.add_argument("--radius", type=float)
    html.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="worker processes used to render independent target pages",
    )
    html.add_argument(
        "--open",
        action="store_true",
        help="open index.html in the default browser",
    )


def _register_assignment_commands(
    subcommands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    assign = add_parser(
        subcommands,
        "assign",
        "Assign one measurement to a contributing target or composite scope.",
        "Creates or updates the current many-to-many assignment and appends an "
        "audit action. This records ownership for future joint fitting but does "
        "not yet change legacy exports.",
    )
    assign.add_argument("measurement_id", type=int)
    assign.add_argument("target")
    assign.add_argument(
        "--role",
        choices=MeasurementTargetRole.choices(),
        default=MeasurementTargetRole.CONTRIBUTOR.value,
    )
    assign.add_argument("--method", default="manual")
    assign.add_argument("--weight", type=float)
    add_actor_argument(assign)
    add_reason_argument(assign)
    unassign = add_parser(
        subcommands,
        "unassign",
        "Remove a current measurement assignment while preserving its history.",
        "Deletes only the materialized current assignment and appends an unassign "
        "action. Provider rows, normalized photometry, and earlier assignment "
        "actions remain intact.",
    )
    unassign.add_argument("measurement_id", type=int)
    unassign.add_argument("target")
    unassign.add_argument(
        "--role",
        choices=MeasurementTargetRole.choices(),
        default=MeasurementTargetRole.CONTRIBUTOR.value,
    )
    add_actor_argument(unassign)
    add_reason_argument(unassign)
    history = add_parser(
        subcommands,
        "assignment-history",
        "List append-only measurement assignment actions.",
        "Shows every assign and unassign action for a target, including actor, "
        "reason, method, role, and optional response weight.",
    )
    history.add_argument("target")


def _register_proposal_commands(subcommands, add_parser) -> None:
    proposals = add_parser(
        subcommands,
        "proposals",
        "Propose system-level measurement contributors without changing the database.",
        "Uses exact identifiers, catalog positions, per-band resolution, hierarchy "
        "semantics, and target lifecycle state. Ambiguous rows remain "
        "review-required; use photometry assign separately to accept a proposal.",
    )
    proposals.add_argument("target")
    proposals.add_argument(
        "--details",
        action="store_true",
        help="include per-measurement proposals; the default is summary-first",
    )
    apply = add_parser(
        subcommands,
        "apply-proposals",
        "Preview or apply conservative high-confidence photometry assignments.",
        "Dry-run is the default and reports what would change for one target system "
        "or sample. Use --apply with --actor to persist only missing high-confidence "
        "assignments; existing conflicts and uncertain proposals remain untouched. "
        "Provider-excluded measurements may be assigned, but retain their exclusion "
        "until an audited photometry include override.",
    )
    apply.add_argument("target", nargs="?")
    apply.add_argument("--sample")
    apply.add_argument(
        "--apply",
        action="store_true",
        help="persist eligible assignments; without this flag the command is read-only",
    )
    apply.add_argument("--actor", help="audit actor; required when --apply is used")
    apply.add_argument(
        "--reason",
        default="accepted high-confidence automatic assignment proposal",
        help="audit reason prefix used for newly applied assignments",
    )
    apply.add_argument(
        "--details",
        action="store_true",
        help="include per-measurement results; the default is summary-first",
    )


def _register_fitting_commands(subcommands, add_parser) -> None:
    fitting = add_parser(
        subcommands,
        "fitting-groups",
        "Read-only joint-fitting groups and assignment views from accepted assignments.",
        "Physical targets are connected only by included measurements assigned to "
        "more than one contributor. Composite targets remain measurement scopes "
        "rather than model nodes; excluded and unresolved measurements are reported "
        "without changing the database. --view full (default) prints the whole "
        "projection; --view readiness summarizes system-level blockers and previews "
        "SIMBAD stellar relatives; --view assignments lists the current "
        "contributor/composite-scope projection for one target.",
    )
    fitting.add_argument("target", nargs="?")
    fitting.add_argument("--sample")
    fitting.add_argument(
        "--view", choices=["full", "readiness", "assignments"], default="full",
    )
    fitting.add_argument(
        "--format",
        choices=["table", "json", "jsonl"],
        default="table",
        help="readiness view only; full and assignments views always print JSON",
    )


def run_photometry_command(context: CliContext) -> int:
    """Run a photometry command against an initialized main database."""

    args = context.args
    try:
        if args.photometry_command in {"exclude", "include", "overrides"}:
            _run_eligibility_command(context)
        elif args.photometry_command in {"review", "review-queue", "review-html"}:
            _run_review_command(context)
        elif args.photometry_command in {"assign", "unassign", "assignment-history"}:
            _run_assignment_command(context)
        elif args.photometry_command in {"proposals", "apply-proposals"}:
            _run_proposal_command(context)
        elif args.photometry_command == "fitting-groups":
            _run_fitting_command(context)
        else:  # pragma: no cover - argparse enforces a registered subcommand
            raise ValueError(f"unknown photometry command: {args.photometry_command}")
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


def _run_eligibility_command(context: CliContext) -> None:
    from .photometry.assignments import (
        list_measurement_eligibility_actions,
        set_measurement_eligibility,
    )

    args = context.args
    sessions = context.require_sessions()
    if args.photometry_command in {"exclude", "include"}:
        value = set_measurement_eligibility(
            sessions,
            args.measurement_id,
            excluded=args.photometry_command == "exclude",
            actor=args.actor,
            reason=args.reason,
        )
        print(context.json({
            "id": value.id,
            "measurement_id": value.measurement_id,
            "excluded": value.excluded,
            "actor": value.actor,
            "reason": value.reason,
            "created_at": value.created_at.isoformat(),
        }, sort_keys=True))
        return
    for value in list_measurement_eligibility_actions(sessions, args.target):
        print(context.json({
            "id": value.id,
            "measurement_id": value.measurement_id,
            "excluded": value.excluded,
            "actor": value.actor,
            "reason": value.reason,
            "created_at": value.created_at.isoformat(),
        }, sort_keys=True))


def _run_review_command(context: CliContext) -> None:
    from .photometry.assignments import photometry_review_queue, review_photometry_associations

    args = context.args
    sessions = context.require_sessions()
    if args.photometry_command == "review":
        print(context.json(
            [
                asdict(value)
                for value in review_photometry_associations(sessions, args.target)
            ],
            sort_keys=True,
        ))
        return

    references = _selected_review_references(context)
    if args.photometry_command == "review-queue":
        values = photometry_review_queue(
            sessions, references, provider=args.provider,
        )
        if args.format == "table":
            print(_format_photometry_review_queue_table(values))
        elif args.format == "json":
            print(context.json(values, sort_keys=True))
        else:
            for value in values:
                print(context.json(value, sort_keys=True))
        return

    _write_review_bundle(context, references)


def _selected_review_references(context: CliContext) -> list[str]:
    args = context.args
    selectors = sum((
        args.target is not None,
        args.sample is not None,
        args.review_all,
    ))
    if selectors != 1:
        raise ValueError("provide exactly one of TARGET, --sample, or --all")
    sessions = context.require_sessions()
    if args.review_all:
        with sessions() as session:
            return list(session.scalars(
                select(Target.sdbid).order_by(Target.sdbid)
            ))
    if args.sample is not None:
        from .samples import SampleService

        return [
            target.sdbid
            for target in SampleService(sessions).members(args.sample)
        ]
    return [args.target]


def _write_review_bundle(context: CliContext, references: list[str]) -> None:
    from .photometry.assignments import photometry_review_queue

    args = context.args
    sessions = context.require_sessions()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        (
            str(context.database_path.resolve()),
            str(reference),
            args.radius,
            str(output_dir / _review_page_filename(str(reference))),
        )
        for reference in references
    ]
    if args.workers == 1 or len(tasks) == 1:
        rendered = list(map(_write_review_page_task, tasks))
    else:
        worker_count = min(args.workers, len(tasks))
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            rendered = list(executor.map(_write_review_page_task, tasks))

    page_by_sdbid = {
        sdbid: filename for sdbid, filename, _output_path in rendered
    }
    target_sdbids = [sdbid for sdbid, _filename, _output_path in rendered]
    review_pages = [output_path for _sdbid, _filename, output_path in rendered]
    values = photometry_review_queue(
        sessions, target_sdbids, provider=args.provider,
    )
    index_path = output_dir / "index.html"
    index_path.write_text(_render_photometry_review_index(
        title="SDB photometry review",
        targets=target_sdbids,
        queue_rows=values,
        page_by_sdbid=page_by_sdbid,
    ))
    if args.open:
        import webbrowser

        webbrowser.open(index_path.resolve().as_uri())
    print(context.json({
        "output_dir": str(output_dir.resolve()),
        "index": str(index_path.resolve()),
        "targets": len(target_sdbids),
        "review_pages": review_pages,
        "queue_rows": len(values),
        "signal_rows": sum(
            1 for value in values if value.get("priority") != "none"
        ),
    }, sort_keys=True))


def _run_assignment_command(context: CliContext) -> None:
    from .photometry.assignments import (
        assign_measurement_target,
        list_measurement_assignment_history,
        unassign_measurement_target,
    )

    args = context.args
    sessions = context.require_sessions()
    if args.photometry_command == "assign":
        value = assign_measurement_target(
            sessions,
            args.measurement_id,
            args.target,
            role=args.role,
            method=args.method,
            weight=args.weight,
            actor=args.actor,
            reason=args.reason,
        )
        print(context.json({
            "association_id": value.id,
            "measurement_id": value.measurement_id,
            "target_id": value.target_id,
            "role": value.role,
            "method": value.method,
            "weight": value.weight,
            "note": value.note,
        }, sort_keys=True))
        return
    if args.photometry_command == "unassign":
        value = unassign_measurement_target(
            sessions,
            args.measurement_id,
            args.target,
            role=args.role,
            actor=args.actor,
            reason=args.reason,
        )
        print(context.json({
            "action_id": value.id,
            "measurement_id": value.measurement_id,
            "target_id": value.target_id,
            "action": value.action,
            "role": value.role,
            "actor": value.actor,
            "reason": value.reason,
        }, sort_keys=True))
        return
    print(context.json([{
        "action_id": value.id,
        "measurement_id": value.measurement_id,
        "target_id": value.target_id,
        "action": value.action,
        "role": value.role,
        "method": value.method,
        "weight": value.weight,
        "actor": value.actor,
        "reason": value.reason,
        "created_at": value.created_at.isoformat(),
    } for value in list_measurement_assignment_history(
        sessions, args.target,
    )], sort_keys=True))


def _run_proposal_command(context: CliContext) -> None:
    args = context.args
    sessions = context.require_sessions()
    if args.photometry_command == "proposals":
        from .photometry.proposals import measurement_assignment_proposals
        from .photometry.reporting import proposal_summary_report

        value = proposal_summary_report(
            measurement_assignment_proposals(sessions, args.target),
            target=args.target,
            include_details=args.details,
        )
    else:
        from .photometry.application import apply_measurement_assignment_proposals
        from .photometry.reporting import without_proposal_items

        value = without_proposal_items(
            apply_measurement_assignment_proposals(
                sessions,
                target_reference=args.target,
                sample=args.sample,
                apply=args.apply,
                actor=args.actor,
                reason=args.reason,
                reporter=context.reporter,
            ),
            include_details=args.details,
        )
    print(context.json(value, sort_keys=True))


def _run_fitting_command(context: CliContext) -> None:
    from .photometry.assignments import list_measurement_target_assignments

    args = context.args
    sessions = context.require_sessions()
    if args.view == "assignments":
        print(context.json(
            list_measurement_target_assignments(sessions, args.target),
            sort_keys=True,
        ))
    elif args.view == "readiness":
        from .photometry.readiness import assignment_readiness_report

        report = assignment_readiness_report(
            sessions,
            target_reference=args.target,
            sample=args.sample,
        )
        if args.format == "table":
            print(_format_assignment_readiness_table(report["rows"]))
        elif args.format == "jsonl":
            for value in report["rows"]:
                print(context.json(value, sort_keys=True))
        else:
            print(context.json(report, sort_keys=True))
    else:
        from .fitting_groups import fitting_group_report

        print(context.json(
            fitting_group_report(
                sessions,
                target_reference=args.target,
                sample=args.sample,
            ),
            sort_keys=True,
        ))


def _review_page_filename(sdbid: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in {"-", "_", ".", "+"} else "_"
        for char in sdbid
    )
    return f"{safe}.html"


def _write_review_page_task(
    task: tuple[str, str, float | None, str],
) -> tuple[str, str, str]:
    """Render one independent review page in a worker process."""

    database, reference, radius_arcsec, output_path = task
    from .database import make_session_factory
    from .review_sky_render import write_review_sky_html
    from .review_widget import build_review_sky_view

    sessions = make_session_factory(database)
    try:
        view = build_review_sky_view(
            sessions,
            reference,
            radius_arcsec=radius_arcsec,
        )
        output = write_review_sky_html(view, output_path)
        return view.sdbid, Path(output).name, str(Path(output).resolve())
    finally:
        bind = sessions.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _render_photometry_review_index(
    *,
    title: str,
    targets: list[str],
    queue_rows: list[dict[str, object]],
    page_by_sdbid: dict[str, str],
) -> str:
    import html

    grouped: dict[str, list[dict[str, object]]] = {sdbid: [] for sdbid in targets}
    for row in queue_rows:
        grouped.setdefault(str(row.get("sdbid") or ""), []).append(row)

    def esc(value) -> str:
        return html.escape("" if value is None else str(value), quote=True)

    def priority_for(rows: list[dict[str, object]]) -> str:
        if not rows:
            return "none"
        return max(
            (str(row.get("priority") or "none") for row in rows),
            key=review_priority_rank,
        )

    table_rows: list[str] = []
    for sdbid in targets:
        rows = grouped.get(sdbid, [])
        priority = priority_for(rows)
        signal_count = sum(1 for row in rows if row.get("priority") != "none")
        signals = sorted({str(row.get("signal") or "") for row in rows if row.get("signal")})
        providers = sorted({str(row.get("provider") or "") for row in rows if row.get("provider")})
        actions = sorted({
            str(row.get("action") or "")
            for row in rows
            if row.get("action") and row.get("action") != "none"
        })
        table_rows.append(f"""
          <tr class="priority-{esc(priority)}">
            <td><a href="{esc(page_by_sdbid[sdbid])}"><code>{esc(sdbid)}</code></a></td>
            <td>{esc(priority)}</td>
            <td>{signal_count}</td>
            <td>{esc(", ".join(providers))}</td>
            <td>{esc("; ".join(signals))}</td>
            <td>{esc("; ".join(actions))}</td>
          </tr>
        """)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{esc(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #111827; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 7px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f8fafc; position: sticky; top: 0; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .muted {{ color: #64748b; }}
    .priority-high td, .priority-highest td {{ background: #fff7ed; }}
    .priority-medium td {{ background: #fefce8; }}
    .priority-low td {{ background: #f8fafc; }}
    .priority-none td {{ color: #64748b; }}
  </style>
</head>
<body>
  <h1>{esc(title)}</h1>
  <p class="muted">Static review bundle. Individual target pages show full sky/context views; this index highlights photometry review queue signals only.</p>
  <table>
    <thead><tr><th>target</th><th>priority</th><th>signals</th><th>providers</th><th>signal summary</th><th>suggested action</th></tr></thead>
    <tbody>{''.join(table_rows)}</tbody>
  </table>
</body>
</html>
"""


def _format_photometry_review_queue_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No photometry review queue rows."
    headers = (
        "priority", "sdbid", "provider", "band", "signal", "predicted", "action",
    )
    rendered = ["  ".join(headers)]
    for row in rows:
        predicted = row.get("predicted_scope") or row.get("predicted_blend_state") or ""
        rendered.append("  ".join([
            str(row.get("priority") or ""),
            str(row.get("sdbid") or ""),
            str(row.get("provider") or ""),
            str(row.get("band") or ""),
            str(row.get("signal") or ""),
            str(predicted),
            str(row.get("action") or ""),
        ]))
    return "\n".join(rendered)


def _format_assignment_readiness_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No system-level assignment blockers."
    columns = (
        ("priority", "priority"),
        ("sdbid", "sdbid"),
        ("role", "role"),
        ("measurement_count", "meas"),
        ("included_measurement_count", "incl"),
        ("detection_count", "det"),
        ("provider_bands", "providers/bands"),
        ("imported_physical_count", "physical"),
        ("importable_relative_count", "import"),
        ("recommended_action", "action"),
    )
    values = []
    for row in rows:
        display = dict(row)
        display["provider_bands"] = ";".join(
            f"{provider['provider']}:{','.join(provider['bands'])}"
            for provider in row["providers"]
        )
        display["imported_physical_count"] = len(row["imported_physical_relatives"])
        values.append([str(display.get(key, "")) for key, _heading in columns])
    headings = [heading for _key, heading in columns]
    widths = [
        max(len(heading), *(len(row[index]) for row in values))
        for index, heading in enumerate(headings)
    ]
    lines = [
        "  ".join(heading.ljust(widths[index]) for index, heading in enumerate(headings)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in values
    )
    return "\n".join(lines)
