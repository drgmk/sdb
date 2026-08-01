"""Single-target, dirty-target, and sample export CLI commands."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
import os
from pathlib import Path
import sys

from .cli_context import CliContext


EXPORT_COMMANDS = {"export", "dirty", "export-dirty", "export-sample"}


def register_export_parsers(commands, add_parser) -> None:
    export = add_parser(
        commands,
        "export",
        "Write one target as an SDF-compatible rawphot file.",
        "Exports current photometry and target metadata into the IPAC-like format "
        "consumed by SDF, preserving exclusion flags for plotting without fitting. "
        "It also writes a versioned joint-fit JSON sidecar; neither operation "
        "refreshes providers.",
    )
    export.add_argument("target")
    export.add_argument("--output", required=True)
    add_parser(
        commands,
        "dirty",
        "List targets with pending export work.",
        "Dirty targets are those whose identity, catalog rows, curated data, "
        "samples, or overrides changed since their last export. Use this before "
        "`export-dirty` to see what would be regenerated.",
    )
    export_dirty = add_parser(
        commands,
        "export-dirty",
        "Export targets currently marked dirty.",
        "Writes rawphot files for targets that need regeneration and clears "
        "export-dirty state for successful files. Use --sample to restrict "
        "regeneration to one sample membership.",
    )
    export_dirty.add_argument("--output-dir", required=True)
    export_dirty.add_argument("--sample")
    export_dirty.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="worker processes used to export independent targets",
    )
    export_sample = add_parser(
        commands,
        "export-sample",
        "Export every member of a sample.",
        "Creates or refreshes rawphot files for all current members, plus the "
        "existing run manifest and a versioned sample-wide joint-fit sidecar. It "
        "skips unchanged rawphot files when possible and records a durable sample "
        "export run.",
    )
    export_sample.add_argument("sample")
    export_sample.add_argument("--output-dir", required=True)
    export_sample.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="worker processes used to export independent targets",
    )


def run_export_command(context: CliContext) -> int:
    args = context.args
    if args.command == "export":
        return _run_target_export(context)
    if args.command == "dirty":
        return _run_dirty_list(context)
    if args.command == "export-dirty":
        return _run_dirty_export(context)
    if args.command == "export-sample":
        return _run_sample_export(context)
    raise ValueError(f"unknown export command: {args.command}")


def _run_target_export(context: CliContext) -> int:
    from .export import export_ipac
    from .joint_fit_manifest import target_manifest_path, write_joint_fit_manifest

    args = context.args
    sessions = context.require_sessions()
    try:
        output = export_ipac(sessions, args.target, args.output)
        write_joint_fit_manifest(
            sessions,
            target_manifest_path(output),
            target_reference=args.target,
            legacy_exports=[{
                "status": "exported",
                "output": str(output.resolve()),
            }],
        )
    except KeyError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(output.resolve())
    return 0


def _run_dirty_list(context: CliContext) -> int:
    from .dirty import pending_export_targets

    for target, event_count, dirty_since in pending_export_targets(
        context.require_sessions()
    ):
        print(context.json({
            "target_id": target.id,
            "sdbid": target.sdbid,
            "event_count": event_count,
            "dirty_since": dirty_since.isoformat(),
        }, sort_keys=True))
    return 0


def _run_dirty_export(context: CliContext) -> int:
    from .dirty import pending_export_targets
    from .sample_export import _export_target_task

    args = context.args
    sessions = context.require_sessions()
    output_dir = Path(args.output_dir)
    if args.workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2
    try:
        pending = pending_export_targets(sessions, sample=args.sample)
    except KeyError as error:
        print(str(error), file=sys.stderr)
        return 2
    tasks = [
        (
            str(context.database_path.expanduser().resolve()),
            target.id,
            target.sdbid,
            str(output_dir / f"{target.sdbid}-rawphot.txt"),
        )
        for target, _event_count, _dirty_since in pending
    ]
    if args.workers == 1 or len(tasks) < 2:
        result_rows = map(_export_target_task, tasks)
    else:
        executor = ProcessPoolExecutor(max_workers=min(args.workers, len(tasks)))
        result_rows = executor.map(_export_target_task, tasks)
    results = []
    try:
        results.extend(context.reporter.iter(
            result_rows,
            desc="Exporting dirty targets",
            total=len(tasks),
            unit="target",
        ))
    finally:
        if args.workers > 1 and len(tasks) >= 2:
            executor.shutdown()
    for row in results:
        print(context.json(row, sort_keys=True))
    exported = sum(row["status"] == "exported" for row in results)
    failed = sum(row["status"] == "failed" for row in results)
    print(context.json({"exported": exported, "failed": failed}, sort_keys=True))
    return 1 if failed else 0


def _run_sample_export(context: CliContext) -> int:
    from .sample_export import SampleExportService

    args = context.args
    try:
        summary = SampleExportService(
            context.require_sessions(),
            reporter=context.reporter,
            workers=args.workers,
        ).export(args.sample, args.output_dir)
    except (KeyError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json(asdict(summary), sort_keys=True))
    return 1 if summary.failed else 0
