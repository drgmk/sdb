"""Provider update and optional post-update export CLI command."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

from .catalogs.registry import SNAPSHOT_CATALOG_PROVIDERS
from .cli_context import CliContext


def register_update_parser(commands, add_parser) -> None:
    update = add_parser(
        commands,
        "update",
        "Fill missing provider results and optionally export targets.",
        "Updates one target, a sample, or all targets without repeating completed "
        "current results unless --force is supplied. It can run bounded workers, "
        "use bulk-capable stages, apply reference snapshots, and write dirty "
        "exports.",
    )
    update.add_argument("target", nargs="?")
    update.add_argument("--all", action="store_true", dest="update_all")
    update.add_argument("--sample")
    update.add_argument("--force", action="store_true")
    update.add_argument("--providers", default="")
    update.add_argument("--workers", type=int, default=4)
    update.add_argument("--chunk-size", type=int, default=500)
    update.add_argument("--export-dir")


def run_update_command(context: CliContext) -> int:
    from .cli_services import build_update_service
    from .dirty import pending_export_targets
    from .export import export_ipac
    from .update import DEFAULT_PROVIDERS

    args = context.args
    sessions = context.require_sessions()
    selectors = sum((
        args.target is not None,
        args.update_all,
        args.sample is not None,
    ))
    if selectors != 1:
        print("provide exactly one of TARGET, --all, or --sample", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2
    if args.chunk_size < 1:
        print("--chunk-size must be at least 1", file=sys.stderr)
        return 2
    providers = tuple(
        value.strip() for value in args.providers.split(",") if value.strip()
    ) or DEFAULT_PROVIDERS
    if args.offline and any(
        provider not in SNAPSHOT_CATALOG_PROVIDERS for provider in providers
    ):
        print(
            "offline update supports only downloaded reference providers",
            file=sys.stderr,
        )
        return 2
    try:
        with context.provider_output():
            service = build_update_service(
                sessions,
                context.reference_database_path,
                workers=args.workers,
                bulk_chunk_size=args.chunk_size,
                offline=args.offline,
                reporter=context.reporter,
            )
            if args.update_all:
                summary = service.update_all(
                    force=args.force, providers=providers,
                )
            elif args.sample is not None:
                from .samples import SampleService

                members = SampleService(sessions).members(args.sample)
                summary = service.update_targets(
                    [target.id for target in members],
                    force=args.force,
                    providers=providers,
                )
            else:
                summary = service.update_target(
                    args.target,
                    force=args.force,
                    providers=providers,
                )
        exports = _export_updated_targets(
            context,
            pending_export_targets=pending_export_targets,
            export_ipac=export_ipac,
        )
        result = asdict(summary)
        result["exports"] = exports
        print(context.json(result, sort_keys=True))
        return 1 if summary.failed else 0
    except (KeyError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2


def _export_updated_targets(
    context: CliContext,
    *,
    pending_export_targets,
    export_ipac,
) -> list[str]:
    args = context.args
    if not args.export_dir:
        return []
    sessions = context.require_sessions()
    output_dir = Path(args.export_dir)
    if args.update_all:
        targets = [value[0] for value in pending_export_targets(sessions)]
    elif args.sample is not None:
        targets = [
            value[0]
            for value in pending_export_targets(sessions, sample=args.sample)
        ]
    else:
        from .targets import resolve_target

        with sessions() as session:
            target = resolve_target(session, args.target)
        dirty_ids = {
            value[0].id for value in pending_export_targets(sessions)
        }
        targets = (
            [target]
            if target is not None and target.id in dirty_ids
            else []
        )
    exports = []
    for target in context.reporter.iter(
        targets,
        desc="Exporting updated targets",
        total=len(targets),
        unit="target",
    ):
        output = export_ipac(
            sessions,
            target.id,
            output_dir / f"{target.sdbid}-rawphot.txt",
        )
        exports.append(str(output.resolve()))
    return exports
