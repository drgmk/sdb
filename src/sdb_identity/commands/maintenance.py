"""Diagnostic and migration-validation CLI commands."""

from __future__ import annotations

import sys

from .context import CliContext


def register_maintenance_parser(commands, add_parser) -> None:
    maintenance = add_parser(
        commands,
        "maintenance",
        "Diagnostic and repair commands outside the normal workflow.",
        "One-off maintenance and migration-validation utilities that are not part "
        "of the routine import/update/export path.",
    )
    subcommands = maintenance.add_subparsers(
        dest="maintenance_command",
        required=True,
    )
    compare_export = add_parser(
        subcommands,
        "compare-export",
        "Compare two rawphot exports for parity checks.",
        "Reads legacy and current rawphot files and reports band/value differences. "
        "This is a diagnostic command for migration validation, not an import path.",
    )
    compare_export.add_argument("legacy")
    compare_export.add_argument("current")


def run_maintenance_command(context: CliContext) -> int:
    from ..parity import compare_exports

    args = context.args
    try:
        result = compare_exports(args.legacy, args.current)
    except (FileNotFoundError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json(result, indent=2, sort_keys=True))
    return 0
