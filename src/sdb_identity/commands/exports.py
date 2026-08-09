"""Fit-package export and dirty-state diagnostics."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import sys

from .context import CliContext


EXPORT_COMMANDS = {"export", "dirty"}


def register_export_parsers(commands, add_parser) -> None:
    export = add_parser(
        commands,
        "export",
        "Export one target, a sample, or all targets as SDF fitting packages.",
        "Reconciles one directory per fitting group beneath the output root. "
        "Each package contains its physical rawphot inputs and joint-fit.json; "
        "unchanged verified files are retained. Choose exactly one of TARGET, "
        "--sample, or --all.",
    )
    export.add_argument("target", nargs="?")
    export.add_argument("--sample")
    export.add_argument("--all", action="store_true", dest="export_all")
    export.add_argument(
        "--output-dir",
        help="fit-package root; defaults to export.root in configuration",
    )
    export.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="worker processes used to export independent targets",
    )
    export.add_argument(
        "--force",
        action="store_true",
        help="rewrite verified rawphot inputs even when their projection is unchanged",
    )
    add_parser(
        commands,
        "dirty",
        "List targets with pending export work.",
        "Dirty targets have authoritative database changes that have not yet "
        "been included in a successful fitting-package export.",
    )


def run_export_command(context: CliContext) -> int:
    if context.args.command == "export":
        return _run_export(context)
    if context.args.command == "dirty":
        return _run_dirty_list(context)
    raise ValueError(f"unknown export command: {context.args.command}")


def _run_export(context: CliContext) -> int:
    from ..package_export import PackageExportService

    args = context.args
    if args.workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2
    try:
        configured_root = context.config.export_root()
        output_dir = (
            Path(args.output_dir).expanduser()
            if args.output_dir is not None
            else configured_root
        )
        if output_dir is None:
            raise ValueError(
                "provide --output-dir or set export.root in configuration"
            )
        summary = PackageExportService(
            context.require_sessions(),
            reporter=context.reporter,
            workers=args.workers,
        ).export(
            output_dir,
            target_reference=args.target,
            sample=args.sample,
            all_targets=args.export_all,
            force=args.force,
        )
    except (KeyError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json(asdict(summary), sort_keys=True))
    return 1 if summary.failed else 0


def _run_dirty_list(context: CliContext) -> int:
    from ..dirty import pending_export_targets

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
