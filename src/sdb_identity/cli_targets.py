"""Target identity, status, history, and note CLI commands."""

from __future__ import annotations

from dataclasses import asdict
import sys

from .cli_context import CliContext
from .service import AddRequest, IdentityService, UnresolvedTarget


TARGET_COMMANDS = {"add", "status", "history", "override-match", "note"}


def register_target_parsers(
    commands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    add = add_parser(
        commands,
        "add",
        "Add one target by name or coordinates.",
        "Names are resolved through SIMBAD unless --offline is active; explicit "
        "coordinates are sufficient even when no remote service knows the source. "
        "Successful additions store submissions, identifiers, astrometry, match "
        "candidates, and provenance atomically.",
    )
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
    status = add_parser(
        commands,
        "status",
        "Show the identity and provider state for one target.",
        "TARGET may be an sdbid or known identifier. The report is intended for "
        "quick inspection before refreshing catalogs, reviewing matches, or "
        "exporting photometry.",
    )
    status.add_argument("target")
    history = add_parser(
        commands,
        "history",
        "Show operator decisions for one target system.",
        "Builds a normalized timeline on demand from the domain-specific action "
        "tables. By default it includes every imported member of the target's "
        "systems.",
    )
    history.add_argument("target")
    history.add_argument("--target-only", action="store_true")
    override = add_parser(
        commands,
        "override-match",
        "Manually accept an identity match candidate.",
        "This records an append-only audit decision and applies the selected "
        "candidate's astrometry and provider identifier to the target. The latest "
        "accepted decision is the submission's sole selection. Use it only after "
        "inspecting `sdb review matches` output.",
    )
    override.add_argument("candidate_id", type=int)
    add_actor_argument(override)
    add_reason_argument(override)
    note = add_parser(
        commands,
        "note",
        "Add or list operator notes for targets.",
        "Notes are lightweight human annotations stored alongside the target "
        "without changing provider data. They are useful for decisions, caveats, "
        "and follow-up reminders.",
    )
    note_commands = note.add_subparsers(dest="note_command", required=True)
    note_add = add_parser(
        note_commands,
        "add",
        "Add an operator note to a target.",
        "Notes are append-only annotations for human context and do not alter "
        "provider rows or exported measurements directly. Include an actor so "
        "later reviews can trace who added the note.",
    )
    note_add.add_argument("target")
    note_add.add_argument("text")
    add_actor_argument(note_add)
    note_list = add_parser(
        note_commands,
        "list",
        "List operator notes for a target.",
        "Shows notes in database order for quick review. Use this before making "
        "manual overrides when the target has known caveats.",
    )
    note_list.add_argument("target")


def run_target_command(context: CliContext) -> int:
    args = context.args
    if args.command == "add":
        return _run_add(context)
    if args.command == "status":
        return _run_status(context)
    if args.command == "history":
        return _run_history(context)
    if args.command == "override-match":
        IdentityService(context.require_sessions()).override_match(
            args.candidate_id,
            actor=args.actor,
            reason=args.reason,
        )
        return 0
    if args.command == "note":
        return _run_note(context)
    raise ValueError(f"unknown target command: {args.command}")


def _run_add(context: CliContext) -> int:
    args = context.args
    sessions = context.require_sessions()
    if args.ensure:
        try:
            if args.offline:
                raise ValueError("--ensure is unavailable in offline mode")
            if args.name is None or args.ra is not None or args.dec is not None:
                raise ValueError("--ensure requires one target name without --ra/--dec")
            from .cli_services import build_update_service
            from .live_providers import AstroqueryGaia, AstroquerySimbad
            from .target_import import TargetImportService
            from .update import DEFAULT_PROVIDERS

            configured = tuple(
                value.strip() for value in args.providers.split(",") if value.strip()
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
                    *context.config.catalog_providers(DEFAULT_PROVIDERS[1:]),
                )
            with context.provider_output():
                result = TargetImportService(
                    sessions,
                    identity_service=IdentityService(
                        sessions,
                        simbad=AstroquerySimbad(),
                        gaia=AstroqueryGaia(),
                    ),
                    update_service=build_update_service(
                        sessions,
                        context.reference_database_path,
                        reporter=context.reporter,
                    ),
                ).import_many(
                    [args.name],
                    providers=providers,
                    command=" ".join(sys.argv),
                )
        except (ValueError, UnresolvedTarget, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(context.json(result.as_dict(), sort_keys=True))
        update_failed = (
            result.update_summary is not None and result.update_summary.failed > 0
        )
        return 1 if result.failed_count or update_failed else 0

    try:
        with context.provider_output():
            from .ingestion import TargetIngestionPlan

            if args.offline:
                service = IdentityService(sessions)
            else:
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
    print(context.json(added.__dict__, sort_keys=True))
    return 0


def _run_status(context: CliContext) -> int:
    from .hierarchy_target_context import HierarchyTargetContextService
    from .models.identity import AstrometricSolution
    from .target_lifecycle import target_lifecycle_status
    from .targets import resolve_targets

    args = context.args
    sessions = context.require_sessions()
    with sessions() as session:
        targets = resolve_targets(session, args.target)
        if not targets:
            print(f"target not found: {args.target}", file=sys.stderr)
            return 1
        for target in targets:
            solution = session.get(
                AstrometricSolution, target.canonical_astrometry_id,
            )
            payload = {
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
            payload["lifecycle"] = asdict(
                target_lifecycle_status(sessions, target.sdbid)
            )
            payload["hierarchy"] = HierarchyTargetContextService(
                sessions,
            ).target_context_summary(target.sdbid)
            print(context.json(payload, sort_keys=True))
    return 0


def _run_history(context: CliContext) -> int:
    from .decision_history import system_decision_history

    args = context.args
    try:
        for value in system_decision_history(
            context.require_sessions(),
            args.target,
            include_system=not args.target_only,
        ):
            print(context.json(value, sort_keys=True))
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


def _run_note(context: CliContext) -> int:
    from .metadata import MetadataService

    args = context.args
    service = MetadataService(context.require_sessions(), None)
    try:
        if args.note_command == "add":
            note = service.add_note(args.target, args.text, actor=args.actor)
            print(context.json({
                "id": note.id,
                "target_id": note.target_id,
                "actor": note.actor,
                "text": note.text,
            }, sort_keys=True))
        else:
            for note in service.list_notes(args.target):
                print(context.json({
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
