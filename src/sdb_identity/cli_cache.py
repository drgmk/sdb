"""Snapshot-cache CLI parser registration and handlers."""

from __future__ import annotations

from dataclasses import asdict
import sys

from .cli_context import CliContext


def register_cache_parser(commands, add_parser) -> None:
    cache = add_parser(
        commands,
        "cache",
        "Inspect the generic provider snapshot cache.",
        "Shows cached raw-provider snapshots used by hierarchy and reference "
        "fetches. These commands are read-only and do not query remote services.",
    )
    cache_commands = cache.add_subparsers(
        dest="cache_command",
        required=True,
    )
    cache_status = add_parser(
        cache_commands,
        "status",
        "List cached provider snapshots.",
        "Reports cached snapshots by provider and catalog, including table "
        "counts, row counts, checksums, source URLs, and fetch times.",
    )
    cache_status.add_argument(
        "--all",
        action="store_true",
        dest="cache_all",
        help="include superseded cached snapshots",
    )
    cache_tables = add_parser(
        cache_commands,
        "tables",
        "List tables in one cached provider snapshot.",
        "Shows table names, row counts, descriptions, and column metadata for "
        "a cached provider catalog. Use --provider if the same catalog ID "
        "exists from more than one provider.",
    )
    cache_tables.add_argument("catalog")
    cache_tables.add_argument("--provider")
    cache_readme = add_parser(
        cache_commands,
        "readme",
        "Print the ReadMe for one cached provider snapshot.",
        "Reads the cached provider ReadMe without making a network request. "
        "Use --provider if the same catalog ID exists from more than one provider.",
    )
    cache_readme.add_argument("catalog")
    cache_readme.add_argument("--provider")
    cache_validate = add_parser(
        cache_commands,
        "validate",
        "Validate one cached provider snapshot.",
        "Checks that a current cached snapshot has source metadata, ReadMe "
        "text, tables, rows, and column metadata. If a matching reference "
        "snapshot exists, its interpreted row count is reported for comparison.",
    )
    cache_validate.add_argument("catalog")
    cache_validate.add_argument("--provider")


def run_cache_command(context: CliContext) -> int:
    from .cache_store import SnapshotCache

    args = context.args
    cache = SnapshotCache(context.cache_database_path)
    try:
        if args.cache_command == "status":
            for value in cache.summaries(include_old=args.cache_all):
                print(context.json(asdict(value), sort_keys=True))
        elif args.cache_command == "tables":
            snapshot = cache.current_snapshot_for_catalog(
                args.catalog,
                provider=args.provider,
            )
            if snapshot is None:
                raise KeyError(f"cached snapshot not found: {args.catalog}")
            for table in snapshot.tables:
                print(
                    context.json(
                        {
                            "provider": snapshot.provider,
                            "catalog": snapshot.catalog_id,
                            "source_id": snapshot.source_id,
                            "table": table.name,
                            "description": table.description,
                            "row_count": len(table.rows),
                            "columns": table.metadata.get("columns", []),
                        },
                        sort_keys=True,
                    )
                )
        elif args.cache_command == "readme":
            snapshot = cache.current_snapshot_for_catalog(
                args.catalog,
                provider=args.provider,
            )
            if snapshot is None:
                raise KeyError(f"cached snapshot not found: {args.catalog}")
            print(snapshot.readme)
        else:
            value = asdict(
                cache.validate(args.catalog, provider=args.provider)
            )
            if context.reference_database_path.exists():
                _add_reference_comparisons(context, value)
            print(context.json(value, sort_keys=True))
            return 0 if value["ok"] else 1
        return 0
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


def _add_reference_comparisons(
    context: CliContext,
    value: dict[str, object],
) -> None:
    from .reference import ReferenceStore
    from .reference_definitions import SNAPSHOT_CATALOGS

    reference = ReferenceStore(context.reference_database_path)
    comparisons = []
    for adapter, definition in SNAPSHOT_CATALOGS.items():
        if definition.catalog != value["catalog_id"]:
            continue
        snapshot = reference.current_snapshot(adapter)
        if snapshot is None:
            continue
        row_count = sum(
            item["row_count"] for item in reference.describe(adapter=adapter)
        )
        comparisons.append(
            {
                "adapter": adapter,
                "snapshot_id": snapshot.id,
                "content_sha256": snapshot.content_sha256,
                "row_count": row_count,
                "row_count_matches_cache": row_count == value["row_count"],
            }
        )
    if comparisons:
        value["reference_snapshots"] = comparisons
