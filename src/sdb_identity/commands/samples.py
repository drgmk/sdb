"""Sample CLI parser registration and handlers."""

from __future__ import annotations

from dataclasses import asdict
import sys

from .context import CliContext


def register_sample_parser(
    commands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    sample = add_parser(
        commands,
        "sample",
        "Create, edit, inspect, and export target samples.",
        "Samples group arbitrary targets and keep small metadata such as date "
        "and note. Membership changes are audited and can drive readiness "
        "checks and sample exports.",
    )
    subcommands = sample.add_subparsers(dest="sample_command", required=True)
    create = add_parser(
        subcommands,
        "create",
        "Create a named target sample.",
        "Samples group targets for update, readiness, and export workflows. "
        "Optional date and note fields capture lightweight provenance about "
        "the sample definition.",
    )
    create.add_argument("name")
    create.add_argument("--date")
    create.add_argument("--note")
    set_metadata = add_parser(
        subcommands,
        "set",
        "Update sample metadata.",
        "Changes the sample date or note without changing membership. "
        "Membership additions and removals use separate audited commands.",
    )
    set_metadata.add_argument("name")
    set_metadata.add_argument("--date")
    set_metadata.add_argument("--note")
    add_parser(
        subcommands,
        "list",
        "List known samples.",
        "Shows sample names and stored metadata. Use this to discover sample "
        "names for update, readiness, or export commands.",
    )
    for action in ("add", "remove"):
        command = add_parser(
            subcommands,
            action,
            f"{action.title()} one target "
            f"{'to' if action == 'add' else 'from'} a sample.",
            "Membership changes are audited with actor and reason. A target "
            "can belong to any number of samples.",
        )
        command.add_argument("name")
        command.add_argument("target")
        add_actor_argument(command)
        add_reason_argument(command)
    members = add_parser(
        subcommands,
        "members",
        "List current members of a sample.",
        "Outputs the targets currently assigned to the sample. This is the "
        "membership source used by sample readiness and sample export.",
    )
    members.add_argument("name")
    readiness = add_parser(
        subcommands,
        "readiness",
        "Check whether a sample is ready for export or review.",
        "Reports missing, ambiguous, failed, dirty, and sample-relevant "
        "unresolved curated state. Database-wide unresolved curated rows "
        "remain a diagnostic count and do not block an unrelated sample. The "
        "command exits non-zero when blockers remain.",
    )
    readiness.add_argument("name")
    readiness.add_argument(
        "--providers",
        default="simbad,gaia_dr3,tycho2,2mass,allwise",
        help="comma-separated providers expected for every sample member",
    )
    import_members = add_parser(
        subcommands,
        "import",
        "Import sample membership from a file.",
        "Adds memberships in bulk while recording actor and reason. The "
        "target identities must already exist or be resolvable by the importer "
        "format.",
    )
    import_members.add_argument("name")
    import_members.add_argument("file")
    add_actor_argument(import_members)
    add_reason_argument(import_members)


def run_sample_command(context: CliContext) -> int:
    from ..samples.service import SampleService

    args = context.args
    sessions = context.require_sessions()
    service = SampleService(sessions)
    try:
        if args.sample_command == "create":
            value = service.create(
                args.name,
                sample_date=args.date,
                note=args.note,
            )
            print(context.json({"id": value.id, "name": value.name}))
        elif args.sample_command == "set":
            value = service.set_metadata(
                args.name,
                sample_date=args.date,
                note=args.note,
            )
            print(context.json({"id": value.id, "name": value.name}))
        elif args.sample_command == "list":
            for value in service.list():
                data = asdict(value)
                data["sample_date"] = (
                    value.sample_date.isoformat() if value.sample_date else None
                )
                print(context.json(data))
        elif args.sample_command in {"add", "remove"}:
            value = getattr(service, args.sample_command)(
                args.name,
                args.target,
                actor=args.actor,
                reason=args.reason,
            )
            print(
                context.json(
                    {
                        "action_id": value.id,
                        "action": value.action,
                        "sample_id": value.sample_id,
                        "target_id": value.target_id,
                    }
                )
            )
        elif args.sample_command == "members":
            for target in service.members(args.name):
                print(
                    context.json(
                        {"target_id": target.id, "sdbid": target.sdbid}
                    )
                )
        elif args.sample_command == "readiness":
            from ..samples.readiness import ReadinessService

            providers = tuple(
                value.strip()
                for value in args.providers.split(",")
                if value.strip()
            )
            report = ReadinessService(sessions).report(
                args.name,
                providers=providers,
            )
            print(context.json(asdict(report)))
            return 1 if report.status == "blocked" else 0
        else:
            value = service.import_members(
                args.name,
                args.file,
                actor=args.actor,
                reason=args.reason,
            )
            print(context.json(value))
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0
