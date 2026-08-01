"""Durable batch-import lifecycle CLI commands."""

from __future__ import annotations

import json
import sys

from .context import CliContext
from ..service import IdentityService


BATCH_COMMANDS = {"import", "import-status", "resume", "retry"}


def register_batch_parsers(commands, add_parser) -> None:
    import_command = add_parser(
        commands,
        "import",
        "Import a CSV batch of target submissions.",
        "The batch importer creates durable per-row work items that can be resumed "
        "or retried. Optional refresh stages run after identity creation with "
        "per-stage worker limits.",
    )
    import_command.add_argument("file")
    import_command.add_argument(
        "--refresh",
        default="",
        help=(
            "comma-separated post-identity stages: "
            "simbad,gaia_dr3,tycho2,2mass,allwise"
        ),
    )
    import_command.add_argument(
        "--workers",
        action="append",
        default=[],
        metavar="STAGE=N",
        help=(
            "worker limit for identity, simbad, gaia_dr3, tycho2, 2mass, "
            "or allwise"
        ),
    )
    status = add_parser(
        commands,
        "import-status",
        "Show the status of a batch import run.",
        "Reports completed, failed, running, and pending work items for a previous "
        "import. Use this before resume or retry to decide what remains.",
    )
    status.add_argument("run_id", type=int)
    resume = add_parser(
        commands,
        "resume",
        "Resume an interrupted batch import run.",
        "Continues pending work from an existing import run without recreating "
        "completed targets or provider results. Worker limits can be supplied "
        "again for the resumed run.",
    )
    resume.add_argument("run_id", type=int)
    resume.add_argument("--workers", action="append", default=[], metavar="STAGE=N")
    retry = add_parser(
        commands,
        "retry",
        "Retry failed items from a batch import run.",
        "By default this retries transient failures only, preserving durable "
        "failure records. Use --failures all when permanent failures have been "
        "fixed by input or code changes.",
    )
    retry.add_argument("run_id", type=int)
    retry.add_argument("--failures", choices=["transient", "all"], default="transient")


def run_batch_command(context: CliContext) -> int:
    args = context.args
    if args.command == "import":
        return _run_import(context)
    if args.command == "import-status":
        return _run_status(context)
    if args.command == "resume":
        return _run_resume(context)
    if args.command == "retry":
        return _run_retry(context)
    raise ValueError(f"unknown batch command: {args.command}")


def _worker_settings(values: list[str]) -> dict[str, int]:
    result = {}
    for value in values:
        try:
            stage, raw_count = value.split("=", 1)
            count = int(raw_count)
        except (ValueError, TypeError) as error:
            raise ValueError(
                f"invalid worker setting: {value}; expected STAGE=N"
            ) from error
        if (
            stage not in {
                "identity", "simbad", "gaia_dr3", "tycho2", "2mass", "allwise",
            }
            or count < 1
        ):
            raise ValueError(f"invalid worker setting: {value}")
        result[stage] = count
    return result


def _batch_service(
    sessions,
    *,
    workers=None,
    offline=False,
    reporter=None,
):
    from ..batch import BatchService
    from ..catalogs.acquisition import CatalogAcquisitionService
    from ..metadata import MetadataService

    if offline:
        identity_factory = lambda: IdentityService(sessions)
        metadata_factory = lambda: MetadataService(sessions, None)
        catalog_factory = lambda: CatalogAcquisitionService(sessions, {})
    else:
        from ..catalogs.registry import (
            REMOTE_CATALOG_PROVIDERS,
            build_catalog_adapters,
        )
        from ..live_providers import AstroqueryGaia, AstroquerySimbad
        from ..simbad_metadata import AstroquerySimbadMetadata

        identity_factory = lambda: IdentityService(
            sessions,
            simbad=AstroquerySimbad(),
            gaia=AstroqueryGaia(),
        )
        metadata_factory = lambda: MetadataService(
            sessions, AstroquerySimbadMetadata()
        )
        catalog_factory = lambda: CatalogAcquisitionService(
            sessions,
            build_catalog_adapters(REMOTE_CATALOG_PROVIDERS),
        )
    return BatchService(
        sessions,
        identity_factory=identity_factory,
        metadata_factory=metadata_factory,
        catalog_factory=catalog_factory,
        workers=workers,
        reporter=reporter,
    )


def _run_import(context: CliContext) -> int:
    args = context.args
    refresh = tuple(
        value.strip() for value in args.refresh.split(",") if value.strip()
    )
    if args.offline and refresh:
        print("remote refresh is unavailable in offline mode", file=sys.stderr)
        return 2
    try:
        with context.provider_output():
            workers = _worker_settings(args.workers)
            service = _batch_service(
                context.require_sessions(),
                workers=workers,
                offline=args.offline,
                reporter=context.reporter,
            )
            created = service.create(args.file, refresh=refresh)
            summary = service.execute(created.run_id)
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json(summary.__dict__, sort_keys=True))
    return 0


def _run_status(context: CliContext) -> int:
    args = context.args
    try:
        summary = _batch_service(
            context.require_sessions(), offline=True,
        ).status(args.run_id)
    except KeyError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json(summary.__dict__, sort_keys=True))
    return 0


def _run_resume(context: CliContext) -> int:
    from ..models.batch import ImportRun

    args = context.args
    sessions = context.require_sessions()
    try:
        workers = _worker_settings(args.workers)
        with sessions() as session:
            run = session.get(ImportRun, args.run_id)
            if run is None:
                raise KeyError(f"import run not found: {args.run_id}")
            stages = tuple(json.loads(run.requested_stages_json))
            if args.offline and any(stage != "identity" for stage in stages):
                raise ValueError(
                    "remote batch stages cannot be resumed in offline mode"
                )
            if not workers:
                workers = json.loads(run.workers_json)
        with context.provider_output():
            summary = _batch_service(
                sessions,
                workers=workers,
                offline=args.offline,
                reporter=context.reporter,
            ).execute(args.run_id)
    except (ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json(summary.__dict__, sort_keys=True))
    return 0


def _run_retry(context: CliContext) -> int:
    args = context.args
    try:
        count = _batch_service(
            context.require_sessions(), offline=True,
        ).retry(args.run_id, failures=args.failures)
    except (ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json({
        "run_id": args.run_id,
        "reset_jobs": count,
    }, sort_keys=True))
    return 0
