"""ALMA CLI parser registration and command handlers."""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import select

from .cli_context import CliContext
from .providers import ProviderError


def register_alma_parser(commands, add_parser) -> None:
    alma = add_parser(
        commands,
        "alma",
        "Manage the local ALMA archive lookup cache.",
        "The ALMA cache stores compact project/member/pointing data used to "
        "find archive projects near a target. Syncing is separate from "
        "photometry updates and can be bootstrapped, resumed, or incrementally "
        "refreshed.",
    )
    alma_commands = alma.add_subparsers(dest="alma_command", required=True)
    alma_sync = add_parser(
        alma_commands,
        "sync",
        "Synchronise ALMA archive observations into the local cache.",
        "Use --bootstrap for a full archive load, --incremental for recent "
        "archive updates, or --resume to continue a previous sync run. The "
        "cache is used for target-nearby project links and is independent of "
        "photometry export.",
    )
    alma_mode = alma_sync.add_mutually_exclusive_group(required=True)
    alma_mode.add_argument("--bootstrap", action="store_true")
    alma_mode.add_argument("--incremental", action="store_true")
    alma_mode.add_argument("--resume", type=int, metavar="RUN_ID")
    alma_sync.add_argument("--start-year", type=int, default=2011)
    alma_sync.add_argument("--end-year", type=int)
    alma_sync.add_argument("--chunk-months", type=int, default=3)
    alma_sync.add_argument("--archive-url")
    alma_sync.add_argument("--timeout", type=float, default=300)
    alma_status = add_parser(
        alma_commands,
        "status",
        "Show recent ALMA sync and cache status.",
        "Reports recent sync runs and chunk progress so long-running archive "
        "updates can be monitored. Use --limit to control how much history is "
        "shown.",
    )
    alma_status.add_argument("--limit", type=int, default=10)
    alma_projects = add_parser(
        alma_commands,
        "projects",
        "List ALMA projects near one target.",
        "Searches cached ALMA pointings near the target position and reports "
        "associated project/member information. It does not contact the ALMA "
        "archive.",
    )
    alma_projects.add_argument("target")
    alma_projects.add_argument("--radius", type=float, default=10.0)


def run_alma_command(context: CliContext) -> int:
    from .alma import AlmaSyncService
    from .alma_lookup import AlmaLookupService

    args = context.args
    sessions = context.require_sessions()
    try:
        if args.alma_command == "sync":
            if context.offline:
                raise ValueError("ALMA sync is unavailable in offline mode")
            with context.provider_output():
                from .alma_transport import AstroqueryAlmaArchive

                service = AlmaSyncService(
                    sessions,
                    AstroqueryAlmaArchive(
                        args.archive_url,
                        timeout_seconds=args.timeout,
                    ),
                )
                if args.bootstrap:
                    summary = service.bootstrap(
                        args.start_year,
                        args.end_year,
                        args.chunk_months,
                    )
                elif args.incremental:
                    summary = service.incremental()
                else:
                    summary = service.resume(
                        args.resume,
                        start_year=args.start_year,
                        end_year=args.end_year,
                        chunk_months=args.chunk_months,
                    )
            print(context.json(asdict(summary), sort_keys=True))
        elif args.alma_command == "projects":
            service = AlmaLookupService(sessions)
            for project in service.projects(args.target, args.radius):
                print(context.json(asdict(project), sort_keys=True))
        else:
            from .models.alma import AlmaSyncRun

            if args.limit < 1:
                raise ValueError("--limit must be at least 1")
            with sessions() as session:
                runs = list(
                    session.scalars(
                        select(AlmaSyncRun)
                        .order_by(AlmaSyncRun.id.desc())
                        .limit(args.limit)
                    )
                )
            for run in runs:
                print(
                    context.json(
                        {
                            "run_id": run.id,
                            "mode": run.mode,
                            "archive_url": run.archive_url,
                            "status": run.status,
                            "row_count": run.row_count,
                            "upserted_count": run.upserted_count,
                            "deactivated_count": run.deactivated_count,
                            "watermark_before": run.watermark_before,
                            "watermark_after": run.watermark_after,
                            "error": run.error,
                        },
                        sort_keys=True,
                    )
                )
    except (KeyError, ValueError, RuntimeError, ProviderError) as error:
        import sys

        print(str(error), file=sys.stderr)
        return 2
    return 0
