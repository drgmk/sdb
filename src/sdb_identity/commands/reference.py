"""Reference-snapshot CLI parser registration and handlers."""

from __future__ import annotations

import json
import sys

from ..catalogs.registry import SNAPSHOT_CATALOG_PROVIDERS
from .context import CliContext
from ..database import make_session_factory
from ..providers import ProviderError


REFERENCE_ADAPTERS = SNAPSHOT_CATALOG_PROVIDERS


def register_reference_parser(commands, add_parser) -> None:
    reference = add_parser(
        commands,
        "reference",
        "Fetch, inspect, apply, and audit whole-catalog reference snapshots.",
        "Reference snapshots store provider tables in a separate SQLite "
        "database for fast local matching. Use these commands for catalogs "
        "where full-table ingestion is preferable to per-target remote queries.",
    )
    subcommands = reference.add_subparsers(
        dest="reference_command",
        required=True,
    )
    fetch = None
    for action in ("fetch", "status", "references", "relationships", "readme"):
        command = add_parser(
            subcommands,
            action,
            f"{action.title()} reference snapshot information.",
            "Reference commands operate on the separate reference SQLite "
            "database. Use fetch to download a snapshot, status/readme/describe "
            "to inspect it, and references/relationships to inspect catalog "
            "metadata.",
        )
        command.add_argument("adapter", choices=REFERENCE_ADAPTERS)
        if action == "fetch":
            fetch = command
    assert fetch is not None
    fetch.add_argument(
        "--refresh-cache",
        action="store_true",
        help="download a fresh raw-provider snapshot before importing",
    )
    fetch.add_argument(
        "--no-cache",
        action="store_true",
        help="fetch directly into the reference database without using the snapshot cache",
    )
    ensure = add_parser(
        subcommands,
        "ensure",
        "Fetch missing or stale configured reference snapshots.",
        "Checks the configured reference provider list, reports current, "
        "missing, and stale snapshots, and fetches only missing or stale "
        "providers. With no provider arguments the [reference] providers list "
        "is used; its default is all.",
    )
    ensure.add_argument(
        "providers",
        nargs="*",
        choices=REFERENCE_ADAPTERS,
        help="optional provider override; defaults to the configured provider list",
    )
    ensure.add_argument(
        "--max-age-days",
        type=float,
        help="override reference.max_age_days from configuration",
    )
    ensure.add_argument(
        "--check",
        action="store_true",
        help="report current, missing, and stale snapshots without fetching",
    )
    describe = add_parser(
        subcommands,
        "describe",
        "Describe tables and columns in a reference snapshot.",
        "Shows VizieR-derived table metadata, column descriptions, units, and "
        "stable local table names. Add a table name to focus on one table.",
    )
    describe.add_argument("adapter", choices=REFERENCE_ADAPTERS)
    describe.add_argument("table", nargs="?")
    apply = add_parser(
        subcommands,
        "apply",
        "Apply a reference snapshot to current targets.",
        "Runs local catalog matching for all targets using a fetched reference "
        "snapshot. Results are versioned like remote catalog refreshes and can "
        "produce matched, no-match, or ambiguous outcomes.",
    )
    apply.add_argument("adapter", choices=REFERENCE_ADAPTERS)
    apply.add_argument("--all", action="store_true", dest="apply_all")
    apply.add_argument("--force", action="store_true")
    audit = add_parser(
        subcommands,
        "audit-identifiers",
        "Audit catalog identifiers against SIMBAD identifiers.",
        "Checks whether position-matched catalog rows with meaningful "
        "identifiers agree with target SIMBAD aliases. Use --problems-only to "
        "focus on conflicts and missing expected identifiers.",
    )
    audit.add_argument("adapter", choices=REFERENCE_ADAPTERS)
    audit.add_argument("--all-targets", action="store_true")
    audit.add_argument("--problems-only", action="store_true")
    for action in ("application-status", "review", "pending"):
        command = add_parser(
            subcommands,
            action,
            f"{action.title()} reference application state.",
            "Use application-status to inspect previous local snapshot "
            "applications, review to inspect unmatched or ambiguous rows, and "
            "pending to see targets needing export after reference changes.",
        )
        command.add_argument("adapter", choices=REFERENCE_ADAPTERS)


def run_reference_command(context: CliContext) -> int:
    from ..reference.application import ReferenceApplicationService
    from ..reference.store import ReferenceStore

    args = context.args
    store = ReferenceStore(context.reference_database_path)
    try:
        if args.reference_command == "fetch":
            with context.provider_output():
                value = store.fetch(
                    args.adapter,
                    cache_path=(
                        None if args.no_cache else context.cache_database_path
                    ),
                    refresh_cache=args.refresh_cache,
                )
            print(context.json(value.__dict__, sort_keys=True))
        elif args.reference_command == "ensure":
            from ..reference.ensure import ensure_reference_snapshots

            providers = (
                tuple(args.providers)
                if args.providers
                else context.config.reference_providers(REFERENCE_ADAPTERS)
            )
            max_age_days = (
                args.max_age_days
                if args.max_age_days is not None
                else context.config.reference_max_age_days()
            )
            with context.provider_output():
                value = ensure_reference_snapshots(
                    store,
                    providers,
                    cache_path=context.cache_database_path,
                    max_age_days=max_age_days,
                    check_only=args.check,
                )
            print(context.json(value, sort_keys=True))
        elif args.reference_command == "status":
            value = _snapshot(store, args.adapter)
            print(
                context.json(
                    {
                        "snapshot_id": value.id,
                        "adapter": value.adapter,
                        "catalog": value.catalog,
                        "content_sha256": value.content_sha256,
                        "source_url": value.source_url,
                        "retrieved_at": value.retrieved_at.isoformat(),
                    },
                    sort_keys=True,
                )
            )
        elif args.reference_command == "describe":
            values = store.describe(adapter=args.adapter)
            if args.table:
                values = [
                    value
                    for value in values
                    if value["name"] == args.table
                    or value["name"].rsplit("/", 1)[-1] == args.table
                ]
                if not values:
                    raise KeyError(f"reference table not found: {args.table}")
            for value in values:
                print(context.json(value, sort_keys=True))
        elif args.reference_command == "relationships":
            for value in store.relationships(adapter=args.adapter):
                print(
                    context.json(
                        {
                            "from_table": value.from_table,
                            "from_column": value.from_column,
                            "to_table": value.to_table,
                            "to_column": value.to_column,
                            "parser": value.parser,
                            "description": value.description,
                        },
                        sort_keys=True,
                    )
                )
        elif args.reference_command == "references":
            for value in store.rows("refs", adapter=args.adapter):
                print(context.json(value, sort_keys=True))
        elif args.reference_command == "readme":
            print(_snapshot(store, args.adapter).readme)
        else:
            _run_application_command(context, store)
    except (KeyError, ValueError, RuntimeError, ProviderError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


def _snapshot(store, adapter: str):
    value = store.current_snapshot(adapter)
    if value is None:
        raise KeyError(f"reference snapshot not found: {adapter}")
    return value


def _run_application_command(context: CliContext, store) -> None:
    from ..reference.application import ReferenceApplicationService

    args = context.args
    path = context.database_path
    if not path.exists():
        raise KeyError(f"database does not exist: {path}; run 'sdb init'")
    sessions = make_session_factory(path)
    application = ReferenceApplicationService(sessions, store)
    if args.reference_command == "apply":
        if not args.apply_all:
            raise ValueError("reference apply requires --all")
        value = application.apply(args.adapter, force=args.force)
        print(context.json(value.__dict__, sort_keys=True))
    elif args.reference_command == "application-status":
        for value in application.runs(args.adapter):
            print(
                context.json(
                    {
                        "application_run_id": value.id,
                        "provider": value.provider,
                        "snapshot_sha256": value.snapshot_sha256,
                        "status": value.status,
                        "targets": value.target_count,
                        "refreshed": value.refreshed_count,
                        "matched": value.match_count,
                        "ambiguous": value.ambiguous_count,
                        "no_match": value.no_match_count,
                        "catalog_rows": value.row_count,
                        "unmatched_rows": value.unmatched_row_count,
                    },
                    sort_keys=True,
                )
            )
    elif args.reference_command == "audit-identifiers":
        from ..identifier_audit import audit_catalog_identifiers

        for value in audit_catalog_identifiers(
            sessions,
            args.adapter,
            include_unmatched=args.all_targets,
        ):
            if args.problems_only and value.status == "agree":
                continue
            print(context.json(value.__dict__, sort_keys=True))
    elif args.reference_command == "review":
        for value in application.unmatched(provider=args.adapter):
            print(
                context.json(
                    {
                        "source_identifier": value.source_identifier,
                        "status": value.status,
                        "candidate_target_ids": json.loads(
                            value.candidate_target_ids_json
                        ),
                        "selected_target_ids": json.loads(
                            value.selected_target_ids_json
                        ),
                    },
                    sort_keys=True,
                )
            )
    else:
        for dirty, target, run in application.pending(args.adapter):
            print(
                context.json(
                    {
                        "application_run_id": run.id,
                        "provider": run.provider,
                        "target_id": target.id,
                        "sdbid": target.sdbid,
                        "reason": dirty.reason,
                    },
                    sort_keys=True,
                )
            )
