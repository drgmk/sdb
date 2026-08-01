from __future__ import annotations

import argparse
import os
import sys

from .cli_alma import register_alma_parser, run_alma_command
from .cli_batch import BATCH_COMMANDS, register_batch_parsers, run_batch_command
from .cli_cache import register_cache_parser, run_cache_command
from .cli_catalogs import (
    CATALOG_COMMANDS,
    register_catalog_parsers,
    run_catalog_command,
)
from .cli_context import CliContext
from .cli_datasets import register_dataset_parser, run_dataset_command
from .cli_exports import EXPORT_COMMANDS, register_export_parsers, run_export_command
from .cli_hierarchy import register_hierarchy_parser, run_hierarchy_command
from .cli_maintenance import register_maintenance_parser, run_maintenance_command
from .cli_photometry import register_photometry_parser, run_photometry_command
from .cli_reference import register_reference_parser, run_reference_command
from .cli_review import REVIEW_COMMANDS, register_review_parsers, run_review_command
from .cli_samples import register_sample_parser, run_sample_command
from .cli_targets import TARGET_COMMANDS, register_target_parsers, run_target_command
from .cli_update import register_update_parser, run_update_command
from .database import init_database, make_session_factory
from .decisions import configured_actor


def _add_parser(subparsers, name: str, summary: str, detail: str, **kwargs):
    return subparsers.add_parser(
        name,
        help=summary,
        description=f"{summary}\n\n{detail}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        **kwargs,
    )


def _add_actor_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--actor",
        default=os.environ.get("SDB_ACTOR"),
        help="audit actor; defaults to SDB_ACTOR",
    )
    command.set_defaults(_operator_audit=True)


def _add_reason_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--reason",
        help="audit reason; a contextual description is generated when omitted",
    )


def _prepare_operator_audit(args: argparse.Namespace) -> None:
    if not getattr(args, "_operator_audit", False):
        return
    args.actor = configured_actor(getattr(args, "actor", None))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="sdb",
        description=(
            "Manage the Python SDB identity, catalog, sample, and export "
            "database. Commands are designed to preserve provenance and record "
            "reviewable decisions rather than editing provider results in place."
        ),
    )
    result.add_argument(
        "--config",
        default=os.environ.get("SDB_CONFIG"),
        help=(
            "TOML configuration file; defaults to SDB_CONFIG, then "
            "~/.config/sdb/config.toml plus ./sdb.toml"
        ),
    )
    result.add_argument("--database", default=os.environ.get("SDB_DATABASE", "sdb.sqlite"))
    result.add_argument(
        "--reference-database",
        default=os.environ.get("SDB_REFERENCE_DATABASE", "sdb-reference.sqlite"),
    )
    result.add_argument(
        "--cache-database",
        default=os.environ.get("SDB_CACHE_DATABASE", "sdb-cache.sqlite"),
    )
    result.add_argument(
        "--compact-json",
        action="store_true",
        help=(
            "write JSON on one line for scripts and JSONL-style consumers; "
            "by default JSON output is indented for interactive review"
        ),
    )
    result.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress interactive progress indicators",
    )
    result.add_argument(
        "--progress",
        action="store_true",
        help=(
            "force progress indicators even when stderr is not detected as an "
            "interactive terminal"
        ),
    )
    result.add_argument("--offline", action="store_true", help="do not query SIMBAD or Gaia")
    commands = result.add_subparsers(dest="command", required=True)
    _add_parser(commands, "init", "Create or migrate the SDB SQLite database.", "Initialises the main database if it does not exist and applies any pending Alembic migrations. Use this before adding targets, and back up existing databases before upgrading.")
    register_target_parsers(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    register_review_parsers(commands, _add_parser)
    register_catalog_parsers(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    register_export_parsers(commands, _add_parser)
    register_maintenance_parser(commands, _add_parser)
    register_update_parser(commands, _add_parser)
    register_alma_parser(commands, _add_parser)
    register_cache_parser(commands, _add_parser)
    register_hierarchy_parser(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    register_sample_parser(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    register_batch_parsers(commands, _add_parser)
    register_photometry_parser(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    register_dataset_parser(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    register_reference_parser(commands, _add_parser)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        from .config import load_config

        args.sdb_config = load_config(args.config)
        args.sdb_config.apply_environment_defaults()
        _prepare_operator_audit(args)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    from .progress import ProgressReporter

    reporter = ProgressReporter.for_cli(quiet=args.quiet, force=args.progress)
    context = CliContext.from_args(args, reporter)
    if args.command == "maintenance":
        return run_maintenance_command(context)
    if args.command == "cache":
        return run_cache_command(context)
    path = context.database_path
    if args.command == "init":
        init_database(path)
        print(path.resolve())
        return 0
    if args.command == "reference":
        return run_reference_command(context)
    if not path.exists():
        print(f"database does not exist: {path}; run 'sdb init'", file=sys.stderr)
        return 2
    sessions = make_session_factory(path)
    context = context.with_sessions(sessions)
    if args.command in TARGET_COMMANDS:
        return run_target_command(context)
    if args.command in CATALOG_COMMANDS:
        return run_catalog_command(context)
    if args.command in REVIEW_COMMANDS:
        return run_review_command(context)
    if args.command in EXPORT_COMMANDS:
        return run_export_command(context)
    if args.command == "update":
        return run_update_command(context)
    if args.command in BATCH_COMMANDS:
        return run_batch_command(context)
    if args.command == "alma":
        return run_alma_command(context)
    if args.command == "hierarchy":
        return run_hierarchy_command(context)
    if args.command == "sample":
        return run_sample_command(context)
    if args.command == "photometry":
        return run_photometry_command(context)
    if args.command == "dataset":
        return run_dataset_command(context)


if __name__ == "__main__":
    raise SystemExit(main())
