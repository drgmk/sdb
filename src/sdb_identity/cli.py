from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select

from .catalog_results import effective_catalog_results
from .catalog_registry import SNAPSHOT_CATALOG_PROVIDERS
from .cli_alma import register_alma_parser, run_alma_command
from .cli_cache import register_cache_parser, run_cache_command
from .cli_context import CliContext
from .cli_datasets import register_dataset_parser, run_dataset_command
from .cli_hierarchy import register_hierarchy_parser, run_hierarchy_command
from .cli_output import (
    format_json as _format_json,
    provider_output_to_stderr as _provider_output_to_stderr,
)
from .cli_photometry import register_photometry_parser, run_photometry_command
from .cli_reference import register_reference_parser, run_reference_command
from .cli_samples import register_sample_parser, run_sample_command
from .database import init_database, make_session_factory
from .decisions import configured_actor
from .models import AstrometricSolution, CatalogRun, MatchCandidate, RawCatalogRow, Target
from .providers import ProviderError
from .service import AddRequest, IdentityService, UnresolvedTarget
from .targets import resolve_target, resolve_targets
from .vocabulary import (
    ProviderRunStatus,
)

REFERENCE_ADAPTERS = SNAPSHOT_CATALOG_PROVIDERS


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
    add = _add_parser(commands, "add", "Add one target by name or coordinates.", "Names are resolved through SIMBAD unless --offline is active; explicit coordinates are sufficient even when no remote service knows the source. Successful additions store submissions, identifiers, astrometry, match candidates, and provenance atomically.")
    add.add_argument("name", nargs="?")
    add.add_argument("--ra", type=float)
    add.add_argument("--dec", type=float)
    add.add_argument("--epoch", type=float, default=2000.0)
    add.add_argument(
        "--ensure",
        action="store_true",
        help=(
            "after adding a named SIMBAD target, fill missing configured "
            "provider coverage and refresh WDS/CCDM target matches"
        ),
    )
    add.add_argument(
        "--providers",
        default="",
        help=(
            "comma-separated --ensure provider override; defaults to SIMBAD "
            "plus [catalog] providers, or use all"
        ),
    )
    status = _add_parser(commands, "status", "Show the identity and provider state for one target.", "TARGET may be an sdbid or known identifier. The report is intended for quick inspection before refreshing catalogs, reviewing matches, or exporting photometry.")
    status.add_argument("target")
    history = _add_parser(commands, "history", "Show operator decisions for one target system.", "Builds a normalized timeline on demand from the domain-specific action tables. By default it includes every imported member of the target's systems.")
    history.add_argument("target")
    history.add_argument("--target-only", action="store_true")
    review = _add_parser(commands, "review", "Inspect review queues or launch the local assignment UI.", "Use this to inspect ambiguous identity/catalog candidates and unresolved IRAS families, or use `review serve --sample NAME` for the localhost-only system-photometry workspace. Queue output is JSON; browser changes require preview followed by an audited apply.")
    review.add_argument("kind", choices=["matches", "catalog-matches", "iras-families", "serve"])
    review.add_argument("--all", action="store_true", dest="review_all")
    review.add_argument("--sample", help="sample readiness queue shown by review serve")
    review.add_argument("--host", default="127.0.0.1", help="review serve bind host (localhost only)")
    review.add_argument("--port", type=int, default=8765, help="review serve TCP port")
    review.add_argument("--open", action="store_true", help="open the local review UI in a browser")
    review_view = _add_parser(commands, "review-view", "Write an interactive sky-view HTML page for one target.", "The Plotly view plots the target, nearby SDB targets, proper-motion arrows, identity candidates, catalog candidate rows, hierarchy candidates, and WDS/CCDM separation/PA geometry. It is read-only for now, but includes candidate/run identifiers needed for future override actions.")
    review_view.add_argument("target")
    review_view.add_argument("--output", required=True)
    review_view.add_argument("--radius", type=float)
    review_view.add_argument("--open", action="store_true", help="open the generated HTML in the default browser")
    override = _add_parser(commands, "override-match", "Manually accept an identity match candidate.", "This records an append-only audit decision and applies the selected candidate's astrometry and provider identifier to the target. The latest accepted decision is the submission's sole selection. Use it only after inspecting `sdb review matches` output.")
    override.add_argument("candidate_id", type=int)
    _add_actor_argument(override)
    _add_reason_argument(override)
    catalog_override = _add_parser(commands, "override-catalog-match", "Manually accept a catalog photometry match candidate.", "This changes the current catalog association through an audited override rather than editing raw provider rows. Use it for ambiguous catalog matches after checking source IDs, separation, and notes.")
    catalog_override.add_argument("candidate_id", type=int)
    _add_actor_argument(catalog_override)
    _add_reason_argument(catalog_override)
    refresh = _add_parser(commands, "refresh", "Refresh one provider for one target.", "Runs a single catalog or metadata provider and stores a versioned result. Previous rows remain available for provenance; current rows are updated only after a successful provider attempt.")
    refresh.add_argument("target")
    refresh.add_argument("--provider", choices=["2mass", "allwise", "gaia_dr3", "tycho2", *REFERENCE_ADAPTERS, "simbad"], required=True)
    runs = _add_parser(commands, "runs", "Show catalog and metadata provider run status for one target.", "Reports which providers matched, failed, returned no match, or are ambiguous, across both catalog photometry and SIMBAD metadata. Each row is tagged with its kind. Add --provider to focus on one provider.")
    runs.add_argument("target")
    runs.add_argument("--provider", choices=["2mass", "allwise", "gaia_dr3", "tycho2", *REFERENCE_ADAPTERS, "simbad"])
    export = _add_parser(commands, "export", "Write one target as an SDF-compatible rawphot file.", "Exports current photometry and target metadata into the IPAC-like format consumed by SDF, preserving exclusion flags for plotting without fitting. It also writes a versioned joint-fit JSON sidecar; neither operation refreshes providers.")
    export.add_argument("target")
    export.add_argument("--output", required=True)
    maintenance = _add_parser(commands, "maintenance", "Diagnostic and repair commands outside the normal workflow.", "One-off maintenance and migration-validation utilities that are not part of the routine import/update/export path.")
    maintenance_commands = maintenance.add_subparsers(dest="maintenance_command", required=True)
    compare_export = _add_parser(maintenance_commands, "compare-export", "Compare two rawphot exports for parity checks.", "Reads legacy and current rawphot files and reports band/value differences. This is a diagnostic command for migration validation, not an import path.")
    compare_export.add_argument("legacy")
    compare_export.add_argument("current")
    update_command = _add_parser(commands, "update", "Fill missing provider results and optionally export targets.", "Updates one target, a sample, or all targets without repeating completed current results unless --force is supplied. It can run bounded workers, use bulk-capable stages, apply reference snapshots, and write dirty exports.")
    update_command.add_argument("target", nargs="?")
    update_command.add_argument("--all", action="store_true", dest="update_all")
    update_command.add_argument("--sample")
    update_command.add_argument("--force", action="store_true")
    update_command.add_argument("--providers", default="")
    update_command.add_argument("--workers", type=int, default=4)
    update_command.add_argument("--chunk-size", type=int, default=500)
    update_command.add_argument("--export-dir")
    _add_parser(commands, "dirty", "List targets with pending export work.", "Dirty targets are those whose identity, catalog rows, curated data, samples, or overrides changed since their last export. Use this before `export-dirty` to see what would be regenerated.")
    export_dirty = _add_parser(commands, "export-dirty", "Export targets currently marked dirty.", "Writes rawphot files for targets that need regeneration and clears export-dirty state for successful files. Use --sample to restrict regeneration to one sample membership.")
    export_dirty.add_argument("--output-dir", required=True)
    export_dirty.add_argument("--sample")
    export_dirty.add_argument(
        "--workers", type=int, default=min(4, os.cpu_count() or 1),
        help="worker processes used to export independent targets",
    )
    export_sample = _add_parser(commands, "export-sample", "Export every member of a sample.", "Creates or refreshes rawphot files for all current members, plus the existing run manifest and a versioned sample-wide joint-fit sidecar. It skips unchanged rawphot files when possible and records a durable sample export run.")
    export_sample.add_argument("sample")
    export_sample.add_argument("--output-dir", required=True)
    export_sample.add_argument(
        "--workers", type=int, default=min(4, os.cpu_count() or 1),
        help="worker processes used to export independent targets",
    )
    register_alma_parser(commands, _add_parser)
    register_cache_parser(commands, _add_parser)
    register_hierarchy_parser(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    attributes = _add_parser(commands, "attributes", "Show current catalog attributes for one target.", "Attributes are non-photometric catalog values such as ages or flags copied from provider rows. They are versioned like photometry and can be filtered by --key.")
    attributes.add_argument("target")
    attributes.add_argument("--key")
    note = _add_parser(commands, "note", "Add or list operator notes for targets.", "Notes are lightweight human annotations stored alongside the target without changing provider data. They are useful for decisions, caveats, and follow-up reminders.")
    note_commands = note.add_subparsers(dest="note_command", required=True)
    note_add = _add_parser(note_commands, "add", "Add an operator note to a target.", "Notes are append-only annotations for human context and do not alter provider rows or exported measurements directly. Include an actor so later reviews can trace who added the note.")
    note_add.add_argument("target")
    note_add.add_argument("text")
    _add_actor_argument(note_add)
    note_list = _add_parser(note_commands, "list", "List operator notes for a target.", "Shows notes in database order for quick review. Use this before making manual overrides when the target has known caveats.")
    note_list.add_argument("target")
    register_sample_parser(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    import_command = _add_parser(commands, "import", "Import a CSV batch of target submissions.", "The batch importer creates durable per-row work items that can be resumed or retried. Optional refresh stages run after identity creation with per-stage worker limits.")
    import_command.add_argument("file")
    import_command.add_argument(
        "--refresh",
        default="",
        help="comma-separated post-identity stages: simbad,gaia_dr3,tycho2,2mass,allwise",
    )
    import_command.add_argument(
        "--workers",
        action="append",
        default=[],
        metavar="STAGE=N",
        help="worker limit for identity, simbad, gaia_dr3, tycho2, 2mass, or allwise",
    )
    import_status = _add_parser(commands, "import-status", "Show the status of a batch import run.", "Reports completed, failed, running, and pending work items for a previous import. Use this before resume or retry to decide what remains.")
    import_status.add_argument("run_id", type=int)
    resume = _add_parser(commands, "resume", "Resume an interrupted batch import run.", "Continues pending work from an existing import run without recreating completed targets or provider results. Worker limits can be supplied again for the resumed run.")
    resume.add_argument("run_id", type=int)
    resume.add_argument("--workers", action="append", default=[], metavar="STAGE=N")
    retry = _add_parser(commands, "retry", "Retry failed items from a batch import run.", "By default this retries transient failures only, preserving durable failure records. Use --failures all when permanent failures have been fixed by input or code changes.")
    retry.add_argument("run_id", type=int)
    retry.add_argument("--failures", choices=["transient", "all"], default="transient")
    register_photometry_parser(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    register_dataset_parser(
        commands, _add_parser, _add_actor_argument, _add_reason_argument,
    )
    register_reference_parser(commands, _add_parser)
    return result


def _worker_settings(values: list[str]) -> dict[str, int]:
    result = {}
    for value in values:
        try:
            stage, raw_count = value.split("=", 1)
            count = int(raw_count)
        except (ValueError, TypeError) as error:
            raise ValueError(f"invalid worker setting: {value}; expected STAGE=N") from error
        if stage not in {"identity", "simbad", "gaia_dr3", "tycho2", "2mass", "allwise"} or count < 1:
            raise ValueError(f"invalid worker setting: {value}")
        result[stage] = count
    return result


def _batch_service(sessions, *, workers=None, offline=False, reporter=None):
    from .batch import BatchService
    from .catalog_acquisition import CatalogAcquisitionService
    from .metadata import MetadataService

    if offline:
        identity_factory = lambda: IdentityService(sessions)
        metadata_factory = lambda: MetadataService(sessions, None)
        catalog_factory = lambda: CatalogAcquisitionService(sessions, {})
    else:
        from .live_providers import AstroqueryGaia, AstroquerySimbad
        from .simbad_metadata import AstroquerySimbadMetadata
        from .catalog_registry import (
            REMOTE_CATALOG_PROVIDERS,
            build_catalog_adapters,
        )

        identity_factory = lambda: IdentityService(
            sessions,
            simbad=AstroquerySimbad(),
            gaia=AstroqueryGaia(),
        )
        metadata_factory = lambda: MetadataService(sessions, AstroquerySimbadMetadata())
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


def _update_service(
    sessions, reference_database, *, workers=4, bulk_chunk_size=500, offline=False,
    reporter=None,
):
    from .catalog_acquisition import CatalogAcquisitionService
    from .metadata import MetadataService
    from .reference import ReferenceStore
    from .update import UpdateService

    if offline:
        metadata_factory = lambda: MetadataService(sessions, None)
        catalog_factory = lambda: CatalogAcquisitionService(sessions, {})
    else:
        from .catalog_registry import (
            REMOTE_CATALOG_PROVIDERS,
            build_catalog_adapters,
        )
        from .simbad_metadata import AstroquerySimbadMetadata

        metadata_factory = lambda: MetadataService(
            sessions, AstroquerySimbadMetadata()
        )
        catalog_factory = lambda: CatalogAcquisitionService(
            sessions, build_catalog_adapters(REMOTE_CATALOG_PROVIDERS),
        )
    return UpdateService(
        sessions,
        ReferenceStore(reference_database),
        metadata_factory=metadata_factory,
        catalog_factory=catalog_factory,
        workers=workers,
        bulk_chunk_size=bulk_chunk_size,
        reporter=reporter,
    )


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
    if args.command == "maintenance" and args.maintenance_command == "compare-export":
        from .parity import compare_exports

        try:
            result = compare_exports(args.legacy, args.current)
        except (FileNotFoundError, OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, result, indent=2, sort_keys=True))
        return 0
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
    if args.command == "history":
        from .decision_history import system_decision_history

        try:
            for value in system_decision_history(
                sessions,
                args.target,
                include_system=not args.target_only,
            ):
                print(_format_json(args, value, sort_keys=True))
        except (KeyError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if args.command == "alma":
        return run_alma_command(context)
    if args.command == "export-sample":
        from .sample_export import SampleExportService

        try:
            summary = SampleExportService(
                sessions, reporter=reporter, workers=args.workers,
            ).export(
                args.sample, args.output_dir,
            )
        except (KeyError, ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, asdict(summary), sort_keys=True))
        return 1 if summary.failed else 0
    if args.command == "hierarchy":
        return run_hierarchy_command(context)
    if args.command == "sample":
        return run_sample_command(context)
    if args.command == "dirty":
        from .dirty import pending_export_targets

        for target, event_count, dirty_since in pending_export_targets(sessions):
            print(_format_json(args, {
                "target_id": target.id,
                "sdbid": target.sdbid,
                "event_count": event_count,
                "dirty_since": dirty_since.isoformat(),
            }, sort_keys=True))
        return 0
    if args.command == "export-dirty":
        from .dirty import pending_export_targets
        from .sample_export import _export_target_task

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
                str(Path(args.database).expanduser().resolve()),
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
            results.extend(reporter.iter(
                result_rows,
                desc="Exporting dirty targets",
                total=len(tasks),
                unit="target",
            ))
        finally:
            if args.workers > 1 and len(tasks) >= 2:
                executor.shutdown()
        for row in results:
            print(_format_json(args, row, sort_keys=True))
        exported = sum(row["status"] == "exported" for row in results)
        failed = sum(row["status"] == "failed" for row in results)
        print(_format_json(args, {"exported": exported, "failed": failed}, sort_keys=True))
        return 1 if failed else 0
    if args.command == "update":
        from .dirty import pending_export_targets
        from .export import export_ipac
        from .update import DEFAULT_PROVIDERS

        selectors = sum((args.target is not None, args.update_all, args.sample is not None))
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
            provider not in REFERENCE_ADAPTERS for provider in providers
        ):
            print(
                "offline update supports only downloaded reference providers",
                file=sys.stderr,
            )
            return 2
        try:
            with _provider_output_to_stderr():
                service = _update_service(
                    sessions,
                    args.reference_database,
                    workers=args.workers,
                    bulk_chunk_size=args.chunk_size,
                    offline=args.offline,
                    reporter=reporter,
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
                        args.target, force=args.force, providers=providers
                    )
            exported = []
            if args.export_dir:
                output_dir = Path(args.export_dir)
                if args.update_all:
                    targets = [value[0] for value in pending_export_targets(sessions)]
                elif args.sample is not None:
                    targets = [
                        value[0] for value in pending_export_targets(
                            sessions, sample=args.sample,
                        )
                    ]
                else:
                    with sessions() as session:
                        target = resolve_target(session, args.target)
                    dirty_ids = {
                        value[0].id for value in pending_export_targets(sessions)
                    }
                    targets = [target] if target is not None and target.id in dirty_ids else []
                for target in reporter.iter(
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
                    exported.append(str(output.resolve()))
            result = asdict(summary)
            result["exports"] = exported
            print(_format_json(args, result, sort_keys=True))
            return 1 if summary.failed else 0
        except (KeyError, ValueError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "add":
        if args.ensure:
            try:
                if args.offline:
                    raise ValueError("--ensure is unavailable in offline mode")
                if args.name is None or args.ra is not None or args.dec is not None:
                    raise ValueError(
                        "--ensure requires one target name without --ra/--dec"
                    )
                from .live_providers import AstroqueryGaia, AstroquerySimbad
                from .target_import import TargetImportService
                from .update import DEFAULT_PROVIDERS

                configured = tuple(
                    value.strip()
                    for value in args.providers.split(",")
                    if value.strip()
                )
                if configured == ("all",):
                    providers = DEFAULT_PROVIDERS
                elif "all" in configured:
                    raise ValueError("--providers may use all only by itself")
                elif configured:
                    providers = configured
                else:
                    providers = (
                        "simbad",
                        *args.sdb_config.catalog_providers(
                            DEFAULT_PROVIDERS[1:]
                        ),
                    )
                with _provider_output_to_stderr():
                    result = TargetImportService(
                        sessions,
                        identity_service=IdentityService(
                            sessions,
                            simbad=AstroquerySimbad(),
                            gaia=AstroqueryGaia(),
                        ),
                        update_service=_update_service(
                            sessions,
                            args.reference_database,
                            reporter=reporter,
                        ),
                    ).import_many(
                        [args.name],
                        providers=providers,
                        command=" ".join(sys.argv),
                    )
            except (ValueError, UnresolvedTarget, RuntimeError) as error:
                print(str(error), file=sys.stderr)
                return 2
            print(_format_json(args, result.as_dict(), sort_keys=True))
            update_failed = (
                result.update_summary is not None
                and result.update_summary.failed > 0
            )
            return 1 if result.failed_count or update_failed else 0
        try:
            with _provider_output_to_stderr():
                from .ingestion import TargetIngestionPlan

                if args.offline:
                    service = IdentityService(sessions)
                else:
                    # Astroquery's Gaia module performs service-status setup at
                    # import time; treat those notices like provider output.
                    from .live_providers import AstroqueryGaia, AstroquerySimbad

                    service = IdentityService(
                        sessions,
                        simbad=AstroquerySimbad(),
                        gaia=AstroqueryGaia(),
                    )
                added = TargetIngestionPlan(identity=service).identify(AddRequest(
                    name=args.name,
                    ra_deg=args.ra,
                    dec_deg=args.dec,
                    epoch=args.epoch,
                    command=" ".join(sys.argv),
                ))
        except (ValueError, UnresolvedTarget) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, added.__dict__, sort_keys=True))
        return 0
    if args.command == "override-match":
        service = IdentityService(sessions)
        service.override_match(args.candidate_id, actor=args.actor, reason=args.reason)
        return 0
    if args.command == "review-view":
        from .review_sky_render import write_review_sky_html
        from .review_widget import build_review_sky_view

        try:
            view = build_review_sky_view(
                sessions,
                args.target,
                radius_arcsec=args.radius,
            )
            output = write_review_sky_html(view, args.output)
            if args.open:
                import webbrowser

                webbrowser.open(output.resolve().as_uri())
        except (KeyError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, {
            "target_id": view.target_id,
            "sdbid": view.sdbid,
            "output": str(output.resolve()),
            "points": len(view.points),
        }, sort_keys=True))
        return 0
    if args.command == "override-catalog-match":
        from .catalog_decisions import CatalogDecisionService

        with sessions() as session:
            raw = session.get(RawCatalogRow, args.candidate_id)
            run = None if raw is None else session.get(CatalogRun, raw.run_id)
        if run is None:
            print("catalog candidate not found", file=sys.stderr)
            return 2
        from .catalog_registry import build_catalog_adapter
        from .reference import ReferenceStore
        try:
            adapter = build_catalog_adapter(
                run.provider,
                reference_store=ReferenceStore(args.reference_database),
            )
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        try:
            value = CatalogDecisionService(
                sessions, {run.provider: adapter}
            ).accept_candidate(
                args.candidate_id, actor=args.actor, reason=args.reason
            )
        except (KeyError, ValueError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, value.__dict__, sort_keys=True))
        return 0
    if args.command == "refresh":
        if args.offline and args.provider not in REFERENCE_ADAPTERS:
            print("remote refresh is unavailable in offline mode", file=sys.stderr)
            return 2
        try:
            with _provider_output_to_stderr():
                from .catalog_registry import CATALOG_PROVIDERS
                if args.provider in CATALOG_PROVIDERS:
                    from .catalog_acquisition import CatalogAcquisitionService
                    from .catalog_registry import build_catalog_adapter
                    from .reference import ReferenceStore
                    adapters = {args.provider: build_catalog_adapter(
                        args.provider,
                        reference_store=ReferenceStore(args.reference_database),
                    )}
                    refreshed = CatalogAcquisitionService(
                        sessions, adapters,
                    ).refresh(
                        args.target, args.provider
                    )
                else:
                    from .metadata import MetadataService
                    from .simbad_metadata import AstroquerySimbadMetadata

                    refreshed = MetadataService(
                        sessions, AstroquerySimbadMetadata(),
                    ).refresh(args.target)
        except (KeyError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, refreshed.__dict__, sort_keys=True))
        return 0
    if args.command == "export":
        from .export import export_ipac
        from .joint_fit_manifest import target_manifest_path, write_joint_fit_manifest

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
    if args.command == "note":
        from .metadata import MetadataService

        service = MetadataService(sessions, None)
        try:
            if args.note_command == "add":
                note = service.add_note(args.target, args.text, actor=args.actor)
                print(_format_json(args, {
                    "id": note.id,
                    "target_id": note.target_id,
                    "actor": note.actor,
                    "text": note.text,
                }, sort_keys=True))
            else:
                for note in service.list_notes(args.target):
                    print(_format_json(args, {
                        "id": note.id,
                        "target_id": note.target_id,
                        "actor": note.actor,
                        "text": note.text,
                        "created_at": note.created_at.isoformat(),
                    }, sort_keys=True))
        except (KeyError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if args.command == "photometry":
        return run_photometry_command(context)
    if args.command == "attributes":
        from .models import CatalogAttribute, MetadataRun, SimbadMetadata

        with sessions() as session:
            target = resolve_target(session, args.target)
            if target is None:
                print("target not found", file=sys.stderr)
                return 2
            matched_run_ids = {
                value.run.id
                for value in effective_catalog_results(
                    session, [target.id],
                ).values()
                if value.status == ProviderRunStatus.MATCH
            }
            query = select(CatalogAttribute).where(
                CatalogAttribute.target_id == target.id,
                CatalogAttribute.run_id.in_(matched_run_ids),
            ).order_by(
                CatalogAttribute.key, CatalogAttribute.provider,
            )
            if args.key:
                query = query.where(CatalogAttribute.key == args.key)
            for value in session.scalars(query):
                print(_format_json(args, {
                    "provider": value.provider,
                    "source_id": value.source_id,
                    "key": value.key,
                    "value_text": value.value_text,
                    "value_float": value.value_float,
                    "uncertainty": value.uncertainty,
                    "unit": value.unit,
                    "quality": value.quality,
                    "reference": value.reference,
                    "note": value.note,
                }, sort_keys=True))
            simbad = session.scalar(
                select(SimbadMetadata)
                .join(MetadataRun, MetadataRun.id == SimbadMetadata.run_id)
                .where(
                    SimbadMetadata.target_id == target.id,
                    MetadataRun.is_current.is_(True),
                    MetadataRun.status == ProviderRunStatus.MATCH,
                )
            )
            if simbad is not None:
                simbad_values = (
                    ("spectral_type", simbad.spectral_type, None, None, simbad.spectral_type_bibcode),
                    ("parallax", None, simbad.parallax_mas, "mas", simbad.parallax_bibcode),
                    ("radial_velocity", None, simbad.radial_velocity_kms, "km/s", simbad.radial_velocity_bibcode),
                )
                for key, text_value, float_value, unit, reference in simbad_values:
                    if args.key and args.key != key:
                        continue
                    if text_value is None and float_value is None:
                        continue
                    print(_format_json(args, {
                        "provider": "simbad",
                        "source_id": simbad.main_id,
                        "key": key,
                        "value_text": text_value,
                        "value_float": float_value,
                        "uncertainty": (
                            simbad.parallax_error_mas if key == "parallax"
                            else simbad.radial_velocity_error_kms if key == "radial_velocity"
                            else None
                        ),
                        "unit": unit,
                        "quality": None,
                        "reference": reference,
                        "note": None,
                    }, sort_keys=True))
        return 0
    if args.command == "dataset":
        return run_dataset_command(context)
    if args.command == "import":
        refresh = tuple(value.strip() for value in args.refresh.split(",") if value.strip())
        if args.offline and refresh:
            print("remote refresh is unavailable in offline mode", file=sys.stderr)
            return 2
        try:
            with _provider_output_to_stderr():
                workers = _worker_settings(args.workers)
                service = _batch_service(
                    sessions,
                    workers=workers,
                    offline=args.offline,
                    reporter=reporter,
                )
                created = service.create(args.file, refresh=refresh)
                summary = service.execute(created.run_id)
        except (OSError, ValueError, KeyError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, summary.__dict__, sort_keys=True))
        return 0
    if args.command == "import-status":
        try:
            summary = _batch_service(sessions, offline=True).status(args.run_id)
        except KeyError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, summary.__dict__, sort_keys=True))
        return 0
    if args.command == "resume":
        try:
            workers = _worker_settings(args.workers)
            from .models import ImportRun

            with sessions() as session:
                run = session.get(ImportRun, args.run_id)
                if run is None:
                    raise KeyError(f"import run not found: {args.run_id}")
                stages = tuple(json.loads(run.requested_stages_json))
                if args.offline and any(stage != "identity" for stage in stages):
                    raise ValueError("remote batch stages cannot be resumed in offline mode")
                if not workers:
                    workers = json.loads(run.workers_json)
            with _provider_output_to_stderr():
                summary = _batch_service(
                    sessions,
                    workers=workers,
                    offline=args.offline,
                    reporter=reporter,
                ).execute(args.run_id)
        except (ValueError, KeyError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, summary.__dict__, sort_keys=True))
        return 0
    if args.command == "retry":
        try:
            count = _batch_service(sessions, offline=True).retry(
                args.run_id,
                failures=args.failures,
            )
        except (ValueError, KeyError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(_format_json(args, {"run_id": args.run_id, "reset_jobs": count}, sort_keys=True))
        return 0
    if args.command == "review" and args.kind == "serve":
        from .catalog_setup import catalog_operator_service_for_provider
        from .reference import ReferenceStore
        from .review_ui import serve_review_ui
        from .update import DEFAULT_PROVIDERS, REMOTE_CATALOGS

        try:
            identity_service_factory = None
            if not args.offline:
                from .live_providers import AstroqueryGaia, AstroquerySimbad

                identity_service_factory = lambda: IdentityService(
                    sessions,
                    simbad=AstroquerySimbad(),
                    gaia=AstroqueryGaia(),
                )
            catalog_coverage_providers = args.sdb_config.catalog_providers(
                DEFAULT_PROVIDERS[1:]
            )
            catalog_update_factory = None
            if not (
                args.offline
                and any(
                    provider in REMOTE_CATALOGS
                    for provider in catalog_coverage_providers
                )
            ):
                catalog_update_factory = lambda: _update_service(
                    sessions,
                    args.reference_database,
                    offline=args.offline,
                )
            serve_review_ui(
                sessions,
                sample=args.sample,
                host=args.host,
                port=args.port,
                open_browser=args.open,
                identity_service_factory=identity_service_factory,
                catalog_service_factory=lambda provider, action: (
                    catalog_operator_service_for_provider(
                        sessions,
                        provider,
                        reference_database=args.reference_database,
                        offline=args.offline,
                        action=action,
                    )
                ),
                catalog_coverage_providers=catalog_coverage_providers,
                catalog_update_factory=catalog_update_factory,
                reference_store=ReferenceStore(args.reference_database),
            )
        except (RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    with sessions() as session:
        if args.command == "status":
            targets = resolve_targets(session, args.target)
            if not targets:
                print(f"target not found: {args.target}", file=sys.stderr)
                return 1
            from .target_lifecycle import target_lifecycle_status
            from .hierarchy_target_context import HierarchyTargetContextService
            for target in targets:
                solution = session.get(AstrometricSolution, target.canonical_astrometry_id)
                status_payload = {
                    "id": target.id,
                    "sdbid": target.sdbid,
                    "ra2000_deg": target.ra2000_deg,
                    "dec2000_deg": target.dec2000_deg,
                    "astrometry": None if solution is None else {
                        "solution_id": solution.id,
                        "source": solution.source,
                        "source_id": solution.source_id,
                        "position_bibcode": solution.position_bibcode,
                        "proper_motion_bibcode": solution.proper_motion_bibcode,
                        "parallax_bibcode": solution.parallax_bibcode,
                        "radial_velocity_bibcode": solution.radial_velocity_bibcode,
                    },
                }
                status_payload["lifecycle"] = asdict(
                    target_lifecycle_status(sessions, target.sdbid)
                )
                status_payload["hierarchy"] = HierarchyTargetContextService(
                    sessions,
                ).target_context_summary(target.sdbid)
                print(_format_json(args, status_payload, sort_keys=True))
            return 0
        if args.command == "runs":
            from .models import MetadataRun

            targets = resolve_targets(session, args.target)
            if not targets:
                print(f"target not found: {args.target}", file=sys.stderr)
                return 1
            want_catalog = args.provider != "simbad"
            want_metadata = args.provider in (None, "simbad")
            for target in targets:
                if want_catalog:
                    effective = effective_catalog_results(
                        session,
                        [target.id],
                        providers=(args.provider,) if args.provider else None,
                    )
                    query = select(CatalogRun).where(CatalogRun.target_id == target.id)
                    if args.provider:
                        query = query.where(CatalogRun.provider == args.provider)
                    for run in session.scalars(query.order_by(CatalogRun.id.desc())):
                        current_result = effective.get(
                            (target.id, run.provider)
                        )
                        print(_format_json(args, {
                            "kind": "catalog",
                            "sdbid": target.sdbid,
                            "run_id": run.id,
                            "provider": run.provider,
                            "release": run.release,
                            "status": run.status,
                            "is_current": run.is_current,
                            "candidate_count": run.candidate_count,
                            "selected_source_id": run.selected_source_id,
                            "effective_status": (
                                current_result.status
                                if current_result is not None
                                and current_result.run.id == run.id
                                else None
                            ),
                            "effective_selected_source_id": (
                                current_result.selected_source_id
                                if current_result is not None
                                and current_result.run.id == run.id
                                else None
                            ),
                            "error": run.error,
                        }, sort_keys=True))
                if want_metadata:
                    query = select(MetadataRun).where(MetadataRun.target_id == target.id)
                    if args.provider:
                        query = query.where(MetadataRun.provider == args.provider)
                    for run in session.scalars(query.order_by(MetadataRun.id.desc())):
                        print(_format_json(args, {
                            "kind": "metadata",
                            "sdbid": target.sdbid,
                            "run_id": run.id,
                            "provider": run.provider,
                            "release": run.release,
                            "status": run.status,
                            "is_current": run.is_current,
                            "query_identifier": run.query_identifier,
                            "candidate_count": run.candidate_count,
                            "error": run.error,
                        }, sort_keys=True))
            return 0
        if args.kind == "iras-families":
            from .models import IrasBandSelection, IrasDetectionFamily

            query = (
                select(IrasDetectionFamily, Target)
                .join(Target, Target.id == IrasDetectionFamily.target_id)
                .where(IrasDetectionFamily.is_current.is_(True))
            )
            if not args.review_all:
                query = query.where(IrasDetectionFamily.status == "review")
            for family, target in session.execute(query.order_by(IrasDetectionFamily.id)):
                selections = list(session.scalars(select(IrasBandSelection).where(
                    IrasBandSelection.family_id == family.id
                ).order_by(IrasBandSelection.band)))
                effective = effective_catalog_results(
                    session, [family.target_id], providers=("iras_psc", "iras_fsc"),
                )
                psc = effective.get((family.target_id, "iras_psc"))
                fsc = effective.get((family.target_id, "iras_fsc"))
                print(_format_json(args, {
                    "family_id": family.id,
                    "target_id": family.target_id,
                    "sdbid": target.sdbid,
                    "status": family.status,
                    "normalized_separation": family.normalized_separation,
                    "reason": family.reason,
                    "psc_run_id": family.psc_run_id,
                    "psc_source_id": (
                        None if psc is None else psc.selected_source_id
                    ),
                    "fsc_run_id": family.fsc_run_id,
                    "fsc_source_id": (
                        None if fsc is None else fsc.selected_source_id
                    ),
                    "band_selections": [{
                        "band": value.band,
                        "selected_measurement_id": value.selected_measurement_id,
                        "alternate_measurement_id": value.alternate_measurement_id,
                        "reason": value.reason,
                    } for value in selections],
                }, sort_keys=True))
        elif args.kind == "catalog-matches":
            current_runs = list(session.scalars(
                select(CatalogRun).where(CatalogRun.is_current.is_(True))
            ))
            effective = effective_catalog_results(
                session, (run.target_id for run in current_runs),
            )
            ambiguous_run_ids = {
                value.run.id
                for value in effective.values()
                if value.status == ProviderRunStatus.AMBIGUOUS
            }
            candidates = session.execute(
                select(RawCatalogRow, CatalogRun, Target)
                .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
                .join(Target, Target.id == CatalogRun.target_id)
                .where(
                    CatalogRun.id.in_(ambiguous_run_ids),
                )
                .order_by(CatalogRun.id, RawCatalogRow.score.desc())
            )
            for candidate, run, target in candidates:
                print(_format_json(args, {
                    "candidate_id": candidate.id,
                    "run_id": run.id,
                    "target_id": run.target_id,
                    "sdbid": target.sdbid,
                    "provider": run.provider,
                    "source_id": candidate.source_id,
                    "separation_arcsec": candidate.separation_arcsec,
                    "score": candidate.score,
                }, sort_keys=True))
        else:
            from .identity_results import effective_identity_candidate_ids
            from .models import Submission

            submissions = list(session.scalars(
                select(Submission)
                .join(MatchCandidate, MatchCandidate.submission_id == Submission.id)
                .group_by(Submission.id)
                .order_by(Submission.id)
            ))
            accepted_ids = effective_identity_candidate_ids(
                session,
                submission_ids=(value.id for value in submissions),
            )
            accepted_submission_ids = set(session.scalars(
                select(MatchCandidate.submission_id).where(
                    MatchCandidate.id.in_(accepted_ids)
                )
            ))
            for submission in submissions:
                if submission.id in accepted_submission_ids:
                    continue
                candidates = session.scalars(
                    select(MatchCandidate)
                    .where(MatchCandidate.submission_id == submission.id)
                    .order_by(MatchCandidate.score.desc())
                )
                print(_format_json(args, {
                    "submission_id": submission.id,
                    "submitted_name": submission.input_name,
                    "submitted_ra_deg": submission.input_ra_deg,
                    "submitted_dec_deg": submission.input_dec_deg,
                    "status": submission.status,
                    "reason": "identity candidates found but none accepted automatically",
                    "candidates": [{
                        "candidate_id": candidate.id,
                        "provider": candidate.provider,
                        "source_id": candidate.source_id,
                        "separation_arcsec": candidate.separation_arcsec,
                        "score": candidate.score,
                    } for candidate in candidates],
                }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
