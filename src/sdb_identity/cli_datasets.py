"""Curated-dataset CLI parser registration and handlers."""

from __future__ import annotations

import sys

from .cli_context import CliContext


DATASETS = ("submm_obs",)


def register_dataset_parser(
    commands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    dataset = add_parser(
        commands,
        "dataset",
        "Import and manage curated source-controlled datasets.",
        "Curated datasets such as submm_obs are reimportable tables maintained "
        "outside remote providers. The commands reconcile records to targets, "
        "review unresolved rows, and control export inclusion.",
    )
    subcommands = dataset.add_subparsers(dest="dataset_command", required=True)
    import_data = add_parser(
        subcommands,
        "import",
        "Import a curated dataset file.",
        "Loads source-controlled curated records such as submm_obs into "
        "versioned dataset tables. Reimports are expected as the file evolves "
        "through manual edits or pull requests.",
    )
    import_data.add_argument("dataset", choices=DATASETS)
    import_data.add_argument("file")
    for action, summary, detail in (
        (
            "status",
            "Show curated dataset import and association status.",
            "Reports the latest import state and unresolved records. Use this "
            "after importing to see whether records need target association review.",
        ),
        (
            "review",
            "List unresolved curated dataset records.",
            "Shows records that could not be confidently associated with "
            "targets. These can be manually associated or left unresolved "
            "until more identity information is available.",
        ),
        (
            "reconcile",
            "Re-run curated dataset association logic.",
            "Attempts to associate current dataset records to targets using "
            "the latest identifiers and astrometry. This is safe to rerun after "
            "adding targets or improving matching rules.",
        ),
    ):
        command = add_parser(subcommands, action, summary, detail)
        command.add_argument("dataset", choices=DATASETS)
    associate = add_parser(
        subcommands,
        "associate",
        "Manually associate one curated record with a target.",
        "Records an audited association for a dataset record that automatic "
        "matching could not resolve. Use record_no from dataset review output.",
    )
    associate.add_argument("dataset", choices=DATASETS)
    associate.add_argument("record_no", type=int)
    associate.add_argument("target")
    add_actor_argument(associate)
    add_reason_argument(associate)
    unassociate = add_parser(
        subcommands,
        "unassociate",
        "Remove a manual curated-record association.",
        "Records an audited unassociation without deleting the curated record. "
        "Use this when a previous association was wrong or superseded.",
    )
    unassociate.add_argument("dataset", choices=DATASETS)
    unassociate.add_argument("record_no", type=int)
    add_actor_argument(unassociate)
    add_reason_argument(unassociate)
    for action in ("exclude", "include"):
        command = add_parser(
            subcommands,
            action,
            f"{action.title()} one curated dataset record for export.",
            "This records an audited inclusion/exclusion decision for curated "
            "photometry. The underlying source-controlled record remains unchanged.",
        )
        command.add_argument("dataset", choices=DATASETS)
        command.add_argument("record_no", type=int)
        add_actor_argument(command)
        add_reason_argument(command)
    pending = add_parser(
        subcommands,
        "pending",
        "List targets with pending curated-data export work.",
        "Shows dataset changes that have not yet flowed through to target "
        "exports. Use this to decide which rawphot files need regeneration.",
    )
    pending.add_argument("dataset", choices=DATASETS)
    mark_exported = add_parser(
        subcommands,
        "mark-exported",
        "Mark curated-data export work complete for a target.",
        "Clears pending curated export state after an external export step. "
        "This is mainly for controlled workflows where export completion is "
        "handled outside `sdb export-dirty`.",
    )
    mark_exported.add_argument("dataset", choices=DATASETS)
    mark_exported.add_argument("target")


def run_dataset_command(context: CliContext) -> int:
    from .datasets import SubmmObsService

    args = context.args
    service = SubmmObsService(context.require_sessions())
    try:
        if args.dataset_command == "import":
            value = service.import_submm_obs(args.file)
            print(context.json(value.__dict__))
        elif args.dataset_command == "status":
            for value in service.revisions(args.dataset):
                print(
                    context.json(
                        {
                            "revision_id": value.id,
                            "dataset": value.dataset,
                            "source_sha256": value.source_sha256,
                            "status": value.status,
                            "is_current": value.is_current,
                            "rows": value.row_count,
                            "new": value.new_count,
                            "changed": value.changed_count,
                            "removed": value.removed_count,
                            "unresolved": value.unresolved_count,
                            "ambiguous": value.ambiguous_count,
                        }
                    )
                )
        elif args.dataset_command == "review":
            for value in service.unresolved(args.dataset):
                print(
                    context.json(
                        {
                            "record_no": value.record_no,
                            "identifier": value.source_identifier,
                            "status": value.association_status,
                            "message": value.association_message,
                        }
                    )
                )
        elif args.dataset_command == "reconcile":
            print(context.json(service.reconcile(args.dataset).__dict__))
        elif args.dataset_command == "associate":
            value = service.associate(
                args.dataset,
                args.record_no,
                args.target,
                actor=args.actor,
                reason=args.reason,
            )
            print(context.json(_association_payload(value)))
        elif args.dataset_command == "unassociate":
            value = service.unassociate(
                args.dataset,
                args.record_no,
                actor=args.actor,
                reason=args.reason,
            )
            print(context.json(_association_payload(value)))
        elif args.dataset_command in {"exclude", "include"}:
            value = service.set_record_override(
                args.dataset,
                args.record_no,
                excluded=args.dataset_command == "exclude",
                actor=args.actor,
                reason=args.reason,
            )
            print(
                context.json(
                    {
                        "action_id": value.id,
                        "measurement_id": value.measurement_id,
                        "excluded": value.excluded,
                    }
                )
            )
        elif args.dataset_command == "pending":
            for dirty, target in service.pending(args.dataset):
                print(
                    context.json(
                        {
                            "dirty_id": dirty.id,
                            "revision_id": (
                                None
                                if dirty.source_id is None
                                else int(dirty.source_id)
                            ),
                            "target_id": target.id,
                            "sdbid": target.sdbid,
                            "reason": dirty.reason,
                        }
                    )
                )
        else:
            count = service.mark_exported(args.dataset, args.target)
            print(context.json({"marked_exported": count}))
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


def _association_payload(value) -> dict[str, object]:
    return {
        "action_id": value.id,
        "dataset": value.dataset,
        "record_no": value.record_no,
        "action": value.action,
        "target_id": value.target_id,
    }
