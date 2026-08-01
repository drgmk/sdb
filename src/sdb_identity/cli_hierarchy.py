"""Hierarchy CLI registration and internally separated command handlers."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import sys

from sqlalchemy import select

from .cli_context import CliContext
from .models.identity import Target
from .service import IdentityService
from .vocabulary import ReviewPriority, TargetRole, TargetState


def register_hierarchy_parser(
    commands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    hierarchy = add_parser(
        commands,
        "hierarchy",
        "Create and inspect target systems and relationships.",
        "Hierarchy records keep binary/multiple-system structure separate from "
        "photometry. Manual, WDS, CCDM, and SIMBAD evidence share the same "
        "reviewable hierarchy projections.",
    )
    subcommands = hierarchy.add_subparsers(
        dest="hierarchy_command",
        required=True,
    )
    _register_target_commands(
        subcommands,
        add_parser,
        add_actor_argument,
        add_reason_argument,
    )
    _register_review_commands(subcommands, add_parser)
    _register_source_commands(subcommands, add_parser)
    _register_graph_commands(
        subcommands,
        add_parser,
        add_actor_argument,
        add_reason_argument,
    )
    _register_matching_commands(
        subcommands,
        add_parser,
        add_actor_argument,
        add_reason_argument,
    )


def _register_target_commands(
    subcommands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    create = add_parser(
        subcommands,
        "create-system",
        "Create a target system.",
        "A system groups related components such as a binary or multiple. The "
        "optional primary target is added as the first system member.",
    )
    create.add_argument("name")
    create.add_argument("--primary")
    create.add_argument("--source", default="manual")
    create.add_argument("--note")
    member = add_parser(
        subcommands,
        "add-member",
        "Add a target to a system.",
        "Membership records component labels such as A, B, AB, Aa, or Ab "
        "without changing target identity.",
    )
    member.add_argument("system")
    member.add_argument("target")
    member.add_argument("--component")
    member.add_argument("--source", default="manual")
    relation = add_parser(
        subcommands,
        "add-relationship",
        "Add an audited target relationship.",
        "Relationships describe parent/child structure or pair-level evidence. "
        "At least one target reference must be supplied.",
    )
    relation.add_argument("--type", required=True, dest="relationship_type")
    relation.add_argument("--system")
    relation.add_argument("--primary")
    relation.add_argument("--secondary")
    relation.add_argument("--parent")
    relation.add_argument("--child")
    relation.add_argument("--component")
    relation.add_argument("--source", default="manual")
    relation.add_argument("--separation", type=float)
    relation.add_argument("--pa", type=float)
    relation.add_argument("--epoch", type=float)
    relation.add_argument("--confidence", default="manual")
    relation.add_argument("--status", default="current")
    relation.add_argument("--actor")
    relation.add_argument("--reason", default="")
    status = add_parser(
        subcommands,
        "status",
        "Show hierarchy context for a target.",
        "Use --scope basic for systems and relationships, provider for matched "
        "provider structure, or system for the full read-only review context.",
    )
    status.add_argument("target")
    status.add_argument(
        "--scope",
        choices=["basic", "provider", "system"],
        default="basic",
    )
    relatives = add_parser(
        subcommands,
        "relatives",
        "Preview immediate SIMBAD relatives and target actions.",
        "Classifies current immediate parent/child rows without importing or "
        "recursively following relationships.",
    )
    relatives.add_argument("target")
    import_relatives = add_parser(
        subcommands,
        "import-relatives",
        "Import immediate stellar SIMBAD relatives.",
        "Imports immediate stellar parents and children, then reconciles "
        "membership, lifecycle roles, and SIMBAD relationship evidence.",
    )
    import_relatives.add_argument("target")
    add_actor_argument(import_relatives)
    add_reason_argument(import_relatives)
    target_state = add_parser(
        subcommands,
        "target-state",
        "Show the current physical/composite role and lifecycle state.",
        "Lifecycle state is an append-only review projection independent of "
        "provider hierarchy evidence.",
    )
    target_state.add_argument("target")
    set_state = add_parser(
        subcommands,
        "set-target-state",
        "Record an audited target role and lifecycle state.",
        "Use physical for fitted targets and composite for measurement scopes "
        "such as AB.",
    )
    set_state.add_argument("target")
    set_state.add_argument("--role", choices=TargetRole.choices(), required=True)
    set_state.add_argument("--state", choices=TargetState.choices(), required=True)
    set_state.add_argument("--superseded-by")
    add_actor_argument(set_state)
    add_reason_argument(set_state)


def _register_review_commands(subcommands, add_parser) -> None:
    review = add_parser(
        subcommands,
        "review-queue",
        "Prioritize hierarchy targets needing review.",
        "Combines hierarchy candidates, accepted decisions, diagnostics, and "
        "photometry blend context. Select one target set with TARGET, --sample, "
        "or --all.",
    )
    review.add_argument("target", nargs="?")
    review.add_argument("--view", choices=["priority", "blend"], default="priority")
    review.add_argument("--sample")
    review.add_argument("--all", action="store_true", dest="review_all")
    review.add_argument("--provider")
    review.add_argument("--min-priority", choices=ReviewPriority.choices())
    review.add_argument("--blended-only", action="store_true")
    review.add_argument("--review-required", action="store_true")
    review.add_argument(
        "--format",
        choices=["table", "jsonl", "json"],
        default="table",
    )


def _register_source_commands(subcommands, add_parser) -> None:
    source = add_parser(
        subcommands,
        "source",
        "Manage imported hierarchy snapshots.",
        "Fetch or import WDS/CCDM snapshots, list stored sources, and prune "
        "duplicate source copies.",
    )
    commands = source.add_subparsers(dest="source_command", required=True)
    fetch = add_parser(
        commands,
        "fetch",
        "Fetch WDS or CCDM into hierarchy records.",
        "Downloads or reuses a cached VizieR catalog, or imports a local "
        "snapshot supplied with --file and --release.",
    )
    fetch.add_argument("provider", choices=["wds", "ccdm"])
    fetch.add_argument("--file")
    fetch.add_argument("--release")
    fetch.add_argument("--note")
    fetch.add_argument("--refresh-cache", action="store_true")
    fetch.add_argument("--no-cache", action="store_true")
    list_sources = add_parser(
        commands,
        "list",
        "List imported hierarchy source snapshots.",
        "Shows provider source snapshots stored in the main database.",
    )
    list_sources.add_argument(
        "--provider", choices=["wds", "ccdm", "simbad", "manual"]
    )
    prune = add_parser(
        commands,
        "prune",
        "Remove duplicate imported hierarchy snapshots.",
        "Keeps the earliest provider/checksum source and removes duplicate "
        "copies plus their derived state.",
    )
    prune.add_argument(
        "--provider", choices=["wds", "ccdm", "simbad", "manual"]
    )


def _register_graph_commands(
    subcommands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    graph = add_parser(
        subcommands,
        "graph",
        "Derive, list, diagnose, and override provider graph edges.",
        "The provider component graph is a reviewable layer separate from "
        "target-level relationships.",
    )
    commands = graph.add_subparsers(dest="graph_command", required=True)
    derive = add_parser(
        commands,
        "derive",
        "Derive provider component graph edges.",
        "Normalizes WDS pair labels into reviewable provider graph edges.",
    )
    derive.add_argument("provider", choices=["wds"])
    derive.add_argument("--source-id", type=int)
    list_edges = add_parser(
        commands,
        "list",
        "List derived hierarchy graph edges.",
        "Shows effective provider graph edges by native ID or target.",
    )
    list_edges.add_argument("reference")
    list_edges.add_argument("--provider", choices=["wds"])
    list_edges.add_argument("--source-id", type=int)
    list_edges.add_argument("--target", action="store_true")
    diagnostics = add_parser(
        commands,
        "diagnostics",
        "List hierarchy graph diagnostics.",
        "Reports structural issues grouped by provider native ID.",
    )
    diagnostics.add_argument("--provider", choices=["wds"])
    diagnostics.add_argument("--source-id", type=int)
    diagnostics.add_argument("--limit", type=int, default=100)
    diagnostics.add_argument("--severity", choices=["review", "info"])
    diagnostics.add_argument("--issue")
    diagnostics.add_argument("--summary", action="store_true")
    override = add_parser(
        commands,
        "override",
        "Override one derived hierarchy graph edge.",
        "Adds an append-only audit record without editing imported rows.",
    )
    override.add_argument("provider", choices=["wds"])
    override.add_argument("native_id")
    override.add_argument("--from", required=True, dest="reference_label")
    override.add_argument("--to", required=True, dest="component_label")
    override.add_argument("--source-id", type=int)
    override.add_argument("--status")
    override.add_argument("--type", dest="relation_type")
    override.add_argument(
        "--role",
        choices=["structural", "non_structural"],
        dest="structural_role",
    )
    add_actor_argument(override)
    add_reason_argument(override)


def _register_matching_commands(
    subcommands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    summary = add_parser(
        subcommands,
        "summary",
        "Summarize hierarchy sources and match candidates.",
        "Reports source, candidate, matched, and ambiguous counts.",
    )
    summary.add_argument(
        "--provider", choices=["wds", "ccdm", "simbad", "manual"]
    )
    summary.add_argument("--source-id", type=int)
    match = add_parser(
        subcommands,
        "match",
        "Match imported WDS or CCDM records to current targets.",
        "Creates auditable hierarchy candidates using identifiers and positions.",
    )
    match.add_argument("provider", choices=["wds", "ccdm"])
    match.add_argument("--source-id", type=int)
    match.add_argument("--radius", type=float, default=30.0)
    candidates = add_parser(
        subcommands,
        "candidates",
        "List hierarchy match candidates.",
        "Prints candidates ordered for review by record and score.",
    )
    candidates.add_argument("--provider", choices=["wds", "ccdm"])
    accept = add_parser(
        subcommands,
        "accept-candidate",
        "Accept a hierarchy match candidate.",
        "Records acceptance and creates source-backed relationship evidence.",
    )
    accept.add_argument("candidate_id", type=int)
    add_actor_argument(accept)
    accept.add_argument("--reason", default="")
    accept.add_argument("--system")
    accept.add_argument("--component")
    accept.add_argument("--type", default="hierarchy_record", dest="relationship_type")
    reject = add_parser(
        subcommands,
        "reject-candidate",
        "Reject a hierarchy match candidate.",
        "Appends a rejection action without deleting provider evidence.",
    )
    reject.add_argument("candidate_id", type=int)
    add_actor_argument(reject)
    add_reason_argument(reject)


def run_hierarchy_command(context: CliContext) -> int:
    from .hierarchy import HierarchyService

    args = context.args
    sessions = context.require_sessions()
    service = HierarchyService(sessions)
    try:
        if args.hierarchy_command == "source":
            _run_source(context, service)
        elif args.hierarchy_command == "graph":
            _run_graph(context, service)
        elif args.hierarchy_command == "review-queue":
            _run_review_queue(context, service)
        elif args.hierarchy_command in {
            "summary",
            "match",
            "candidates",
            "accept-candidate",
            "reject-candidate",
        }:
            _run_matching(context, service)
        else:
            _run_target_state(context, service)
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


def _run_source(context: CliContext, service) -> None:
    args = context.args
    if args.source_command == "fetch":
        if args.file is not None:
            if args.release is None:
                raise ValueError("--release is required when importing from --file")
            value = service.import_snapshot(
                args.provider,
                args.file,
                release=args.release,
                note=args.note,
            )
        else:
            with context.provider_output():
                value = service.fetch_snapshot(
                    args.provider,
                    cache_path=(
                        None if args.no_cache else context.cache_database_path
                    ),
                    refresh_cache=args.refresh_cache,
                    release=args.release,
                    note=args.note,
                )
        print(context.json(asdict(value)))
    elif args.source_command == "list":
        for value in service.sources(args.provider):
            print(
                context.json(
                    {
                        "source_id": value.id,
                        "provider": value.provider,
                        "release": value.release,
                        "source_file": value.source_file,
                        "checksum": value.checksum,
                        "fetched_at": (
                            None
                            if value.fetched_at is None
                            else value.fetched_at.isoformat()
                        ),
                        "imported_at": value.imported_at.isoformat(),
                        "note": value.note,
                    }
                )
            )
    else:
        print(context.json(asdict(service.prune_duplicate_sources(args.provider))))


def _run_graph(context: CliContext, service) -> None:
    args = context.args
    if args.graph_command == "derive":
        print(
            context.json(
                asdict(
                    service.derive_graph(
                        args.provider,
                        source_id=args.source_id,
                    )
                )
            )
        )
    elif args.graph_command == "list":
        for value in service.graph_edges(
            provider=args.provider,
            native_id=None if args.target else args.reference,
            target=args.reference if args.target else None,
            source_id=args.source_id,
        ):
            print(context.json(asdict(value)))
    elif args.graph_command == "diagnostics":
        rows = service.graph_diagnostics(
            provider=args.provider,
            source_id=args.source_id,
            limit=0 if args.summary else args.limit,
            severity=args.severity,
            issue=args.issue,
        )
        if args.summary:
            counts = Counter((row.severity, row.issue) for row in rows)
            for (severity, issue), count in sorted(
                counts.items(),
                key=lambda item: (
                    0 if item[0][0] == "review" else 1,
                    item[0][1],
                ),
            ):
                print(
                    context.json(
                        {"count": count, "issue": issue, "severity": severity}
                    )
                )
        else:
            for value in rows:
                print(context.json(asdict(value)))
    else:
        value = service.override_graph_edge(
            provider=args.provider,
            native_id=args.native_id,
            reference_label=args.reference_label,
            component_label=args.component_label,
            source_id=args.source_id,
            status=args.status,
            relation_type=args.relation_type,
            structural_role=args.structural_role,
            actor=args.actor,
            reason=args.reason,
        )
        print(context.json(asdict(value)))


def _run_review_queue(context: CliContext, service) -> None:
    args = context.args
    sessions = context.require_sessions()
    selectors = sum(
        (args.target is not None, args.sample is not None, args.review_all)
    )
    if selectors != 1:
        raise ValueError("provide exactly one of TARGET, --sample, or --all")
    if args.review_all:
        with sessions() as session:
            references = list(
                session.scalars(select(Target.sdbid).order_by(Target.sdbid))
            )
    elif args.sample is not None:
        from .samples import SampleService

        references = [
            target.sdbid
            for target in SampleService(sessions).members(args.sample)
        ]
    else:
        references = [args.target]
    if args.view == "blend":
        rows = service.photometry_review(
            references,
            provider=args.provider,
            blended_only=args.blended_only,
            review_required=args.review_required,
        )
        table = _format_photometry_review_table
    else:
        rows = service.review_queue(
            references,
            provider=args.provider,
            min_priority=args.min_priority,
        )
        table = _format_review_queue_table
    if args.format == "table":
        print(table(rows))
    elif args.format == "json":
        print(context.json(rows))
    else:
        for value in rows:
            print(context.json(value))


def _run_matching(context: CliContext, service) -> None:
    args = context.args
    if args.hierarchy_command == "summary":
        print(
            context.json(
                service.summary(args.provider, source_id=args.source_id)
            )
        )
    elif args.hierarchy_command == "match":
        print(
            context.json(
                asdict(
                    service.match_records(
                        args.provider,
                        source_id=args.source_id,
                        radius_arcsec=args.radius,
                    )
                )
            )
        )
    elif args.hierarchy_command == "candidates":
        for value in service.review_matches(args.provider):
            print(context.json(asdict(value)))
    elif args.hierarchy_command == "accept-candidate":
        value = service.accept_match(
            args.candidate_id,
            actor=args.actor,
            reason=args.reason,
            system=args.system,
            component_label=args.component,
            relationship_type=args.relationship_type,
        )
        print(context.json(asdict(value)))
    else:
        value = service.reject_match(
            args.candidate_id,
            actor=args.actor,
            reason=args.reason,
        )
        print(context.json(asdict(value)))


def _run_target_state(context: CliContext, service) -> None:
    args = context.args
    sessions = context.require_sessions()
    if args.hierarchy_command == "create-system":
        value = service.create_system(
            args.name,
            primary=args.primary,
            source=args.source,
            note=args.note,
        )
        print(
            context.json(
                {
                    "system_id": value.id,
                    "name": value.name,
                    "primary_target_id": value.primary_target_id,
                    "source": value.source,
                }
            )
        )
    elif args.hierarchy_command == "add-member":
        value = service.add_member(
            args.system,
            args.target,
            component_label=args.component,
            source=args.source,
        )
        print(
            context.json(
                {
                    "member_id": value.id,
                    "system_id": value.system_id,
                    "target_id": value.target_id,
                    "component_label": value.component_label,
                    "source": value.source,
                }
            )
        )
    elif args.hierarchy_command == "add-relationship":
        value = service.add_relationship(
            relationship_type=args.relationship_type,
            primary=args.primary,
            secondary=args.secondary,
            parent=args.parent,
            child=args.child,
            system=args.system,
            component=args.component,
            source=args.source,
            separation_arcsec=args.separation,
            pa_deg=args.pa,
            relation_epoch=args.epoch,
            confidence=args.confidence,
            status=args.status,
            actor=args.actor,
            reason=args.reason,
        )
        print(context.json(_relationship_payload(value)))
    elif args.hierarchy_command == "relatives":
        from .system_expansion import preview_immediate_relatives

        print(context.json(preview_immediate_relatives(sessions, args.target)))
    elif args.hierarchy_command == "import-relatives":
        if context.offline:
            raise ValueError(
                "hierarchy import-relatives is unavailable in offline mode"
            )
        with context.provider_output():
            from .live_providers import AstroqueryGaia, AstroquerySimbad
            from .system_expansion import import_immediate_relatives

            identity = IdentityService(
                sessions,
                simbad=AstroquerySimbad(),
                gaia=AstroqueryGaia(),
            )
            value = import_immediate_relatives(
                sessions,
                args.target,
                identity_service=identity,
                actor=args.actor,
                reason=args.reason,
            )
        print(context.json(value.as_dict()))
    elif args.hierarchy_command == "target-state":
        from .target_lifecycle import target_lifecycle_status

        print(context.json(asdict(target_lifecycle_status(sessions, args.target))))
    elif args.hierarchy_command == "set-target-state":
        from .target_lifecycle import (
            set_target_lifecycle,
            target_lifecycle_status,
        )

        set_target_lifecycle(
            sessions,
            args.target,
            role=args.role,
            state=args.state,
            superseded_by=args.superseded_by,
            actor=args.actor,
            reason=args.reason,
        )
        print(context.json(asdict(target_lifecycle_status(sessions, args.target))))
    elif args.scope == "provider":
        print(context.json(service.target_context(args.target)))
    elif args.scope == "system":
        print(context.json(service.system_context(args.target)))
    else:
        print(context.json(service.status(args.target).as_dict()))


def _relationship_payload(value) -> dict[str, object]:
    if value.direction == "a_parent_b":
        parent_id = value.endpoint_a_target_id
        child_id = value.endpoint_b_target_id
        primary_id = secondary_id = None
    elif value.direction == "b_parent_a":
        parent_id = value.endpoint_b_target_id
        child_id = value.endpoint_a_target_id
        primary_id = secondary_id = None
    else:
        primary_id = value.endpoint_a_target_id
        secondary_id = value.endpoint_b_target_id
        parent_id = child_id = None
    return {
        "relationship_id": value.id,
        "relationship_type": value.relation_type,
        "system_id": value.system_id,
        "parent_target_id": parent_id,
        "child_target_id": child_id,
        "primary_target_id": primary_id,
        "secondary_target_id": secondary_id,
        "source": value.source,
        "status": value.status,
    }


def _format_photometry_review_table(rows: list[dict[str, object]]) -> str:
    return _format_table(
        rows,
        [
            ("sdbid", "sdbid"),
            ("classification", "class"),
            ("component_assignment_status", "assignment"),
            ("hierarchy_decision_basis", "basis"),
            ("target_level", "level"),
            ("nearest_pair_arcsec", 'sep(")'),
            ("measurement_count", "n"),
            ("predicted_scope_counts", "scope"),
            ("likely_blended_bands", "blended bands"),
            ("recommendation", "recommendation"),
        ],
        "No matching photometry review rows.",
    )


def _format_review_queue_table(rows: list[dict[str, object]]) -> str:
    return _format_table(
        rows,
        [
            ("priority", "priority"),
            ("sdbid", "sdbid"),
            ("basis", "basis"),
            ("candidate_count", "cand"),
            ("accepted_count", "acc"),
            ("likely_blended_bands", "blended bands"),
            ("nearest_pair_arcsec", 'sep(")'),
            ("reason", "reason"),
        ],
        "No matching hierarchy review queue rows.",
    )


def _format_table(
    rows: list[dict[str, object]],
    columns: list[tuple[str, str]],
    empty: str,
) -> str:
    values = [
        [_format_table_value(key, row.get(key)) for key, _ in columns]
        for row in rows
    ]
    if not values:
        return empty
    headings = [heading for _, heading in columns]
    widths = [
        max(len(heading), *(len(row[index]) for row in values))
        for index, heading in enumerate(headings)
    ]
    lines = [
        "  ".join(
            heading.ljust(widths[index])
            for index, heading in enumerate(headings)
        ),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in values
    )
    return "\n".join(lines)


def _format_table_value(key: str, value: object) -> str:
    if value is None:
        return "-"
    if key == "nearest_pair_arcsec":
        return f"{float(value):.2f}"
    if key == "likely_blended_bands":
        values = list(value or [])
        return (
            "-"
            if not values
            else ",".join(str(item).split(":", 1)[-1] for item in values)
        )
    if key in {"predicted_scope_counts", "predicted_blend_counts"}:
        values = dict(value or {})
        return (
            "-"
            if not values
            else ",".join(
                f"{name}:{count}" for name, count in sorted(values.items())
            )
        )
    return str(value)
