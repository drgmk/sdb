from __future__ import annotations

import argparse
from collections import Counter
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
from .cli_output import (
    format_json as _format_json,
    provider_output_to_stderr as _provider_output_to_stderr,
)
from .cli_reference import register_reference_parser, run_reference_command
from .cli_samples import register_sample_parser, run_sample_command
from .database import init_database, make_session_factory
from .decisions import configured_actor
from .models import AstrometricSolution, CatalogRun, MatchCandidate, RawCatalogRow, Target
from .providers import ProviderError
from .service import AddRequest, IdentityService, UnresolvedTarget
from .targets import resolve_target, resolve_targets
from .vocabulary import (
    MeasurementTargetRole,
    ProviderRunStatus,
    ReviewPriority,
    TargetRole,
    TargetState,
    review_priority_rank,
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
    hierarchy = _add_parser(commands, "hierarchy", "Create and inspect target systems and relationships.", "Hierarchy records keep binary/multiple-system structure separate from photometry. This first layer supports manual systems and relationships; WDS, CCDM, and SIMBAD imports can later write the same tables.")
    hierarchy_commands = hierarchy.add_subparsers(dest="hierarchy_command", required=True)
    hierarchy_create = _add_parser(hierarchy_commands, "create-system", "Create a target system.", "A system groups related components such as a binary or multiple. The optional primary target is also added as the first system member and marked export-dirty.")
    hierarchy_create.add_argument("name")
    hierarchy_create.add_argument("--primary")
    hierarchy_create.add_argument("--source", default="manual")
    hierarchy_create.add_argument("--note")
    hierarchy_member = _add_parser(hierarchy_commands, "add-member", "Add a target to a system.", "Membership records component labels such as A, B, AB, Aa, or Ab without changing the target identity itself.")
    hierarchy_member.add_argument("system")
    hierarchy_member.add_argument("target")
    hierarchy_member.add_argument("--component")
    hierarchy_member.add_argument("--source", default="manual")
    hierarchy_relation = _add_parser(hierarchy_commands, "add-relationship", "Add an audited target relationship.", "Relationships can describe parent/child structure or pair-level evidence from WDS, CCDM, SIMBAD, or manual review. At least one target reference must be supplied.")
    hierarchy_relation.add_argument("--type", required=True, dest="relationship_type")
    hierarchy_relation.add_argument("--system")
    hierarchy_relation.add_argument("--primary")
    hierarchy_relation.add_argument("--secondary")
    hierarchy_relation.add_argument("--parent")
    hierarchy_relation.add_argument("--child")
    hierarchy_relation.add_argument("--component")
    hierarchy_relation.add_argument("--source", default="manual")
    hierarchy_relation.add_argument("--separation", type=float)
    hierarchy_relation.add_argument("--pa", type=float)
    hierarchy_relation.add_argument("--epoch", type=float)
    hierarchy_relation.add_argument("--confidence", default="manual")
    hierarchy_relation.add_argument("--status", default="current")
    hierarchy_relation.add_argument("--actor")
    hierarchy_relation.add_argument("--reason", default="")
    hierarchy_status = _add_parser(hierarchy_commands, "status", "Show hierarchy context for a target.", "Reports hierarchy context for one target. --scope basic (default) reports systems, memberships, and relationships; --scope provider adds matched WDS/CCDM systems, derived component geometry, and graph diagnostics; --scope system adds nearby SDB targets, identity cross-candidates, and current photometry for a full read-only review report.")
    hierarchy_status.add_argument("target")
    hierarchy_status.add_argument("--scope", choices=["basic", "provider", "system"], default="basic")
    hierarchy_relatives = _add_parser(hierarchy_commands, "relatives", "Preview immediate SIMBAD relatives and whether they should become targets.", "Classifies current parent/child rows as already imported, importable stellar structure, contextual-only, or review-required. This is read-only and never follows relationships recursively.")
    hierarchy_relatives.add_argument("target")
    hierarchy_import_relatives = _add_parser(hierarchy_commands, "import-relatives", "Import immediate stellar SIMBAD relatives and reconcile one target system.", "Imports only current immediate stellar parents and children, then records component membership, lifecycle roles, and SIMBAD parent/child evidence. Clusters, moving groups, planets, unknown types, and relatives of newly added targets are not expanded.")
    hierarchy_import_relatives.add_argument("target")
    _add_actor_argument(hierarchy_import_relatives)
    _add_reason_argument(hierarchy_import_relatives)
    hierarchy_target_state = _add_parser(hierarchy_commands, "target-state", "Show the current physical/composite role and lifecycle state.", "Existing targets default to unspecified/active. States are append-only review decisions and do not yet suppress or otherwise change legacy exports.")
    hierarchy_target_state.add_argument("target")
    hierarchy_set_target_state = _add_parser(hierarchy_commands, "set-target-state", "Record an audited target role and lifecycle state.", "Use physical for fitted stellar components and composite for scopes such as AB. Suppressed, archived, system-only, and superseded states are recorded now but do not alter export until an explicit policy is enabled.")
    hierarchy_set_target_state.add_argument("target")
    hierarchy_set_target_state.add_argument(
        "--role", choices=TargetRole.choices(), required=True,
    )
    hierarchy_set_target_state.add_argument(
        "--state", choices=TargetState.choices(), required=True,
    )
    hierarchy_set_target_state.add_argument("--superseded-by")
    _add_actor_argument(hierarchy_set_target_state)
    _add_reason_argument(hierarchy_set_target_state)
    hierarchy_review_queue = _add_parser(hierarchy_commands, "review-queue", "Prioritize hierarchy targets needing review.", "Combines hierarchy candidates, accepted decisions, diagnostics, and photometry blend context for a target set. --view priority (default) ranks targets by review priority; --view blend lists hierarchy/blend photometry context per band. Select exactly one target set with TARGET, --sample, or --all.")
    hierarchy_review_queue.add_argument("target", nargs="?")
    hierarchy_review_queue.add_argument("--view", choices=["priority", "blend"], default="priority")
    hierarchy_review_queue.add_argument("--sample")
    hierarchy_review_queue.add_argument("--all", action="store_true", dest="review_all")
    hierarchy_review_queue.add_argument("--provider")
    hierarchy_review_queue.add_argument(
        "--min-priority",
        choices=ReviewPriority.choices(),
        help="priority view only",
    )
    hierarchy_review_queue.add_argument("--blended-only", action="store_true", help="blend view only")
    hierarchy_review_queue.add_argument("--review-required", action="store_true", help="blend view only")
    hierarchy_review_queue.add_argument(
        "--format",
        choices=["table", "jsonl", "json"],
        default="table",
        help="output format; table is intended for interactive review",
    )
    hierarchy_summary = _add_parser(hierarchy_commands, "summary", "Summarize hierarchy sources and match candidates.", "Reports source record counts, candidate counts by status and method, and matched/ambiguous record counts. Use this after WDS/CCDM fetch and match to assess review workload.")
    hierarchy_summary.add_argument("--provider", choices=["wds", "ccdm", "simbad", "manual"])
    hierarchy_summary.add_argument("--source-id", type=int)
    hierarchy_match = _add_parser(hierarchy_commands, "match", "Match imported WDS or CCDM records to current targets.", "Creates auditable hierarchy match candidates using existing external identifiers first and nearby epoch-2000 positions as supporting evidence. This does not create systems or relationships.")
    hierarchy_match.add_argument("provider", choices=["wds", "ccdm"])
    hierarchy_match.add_argument("--source-id", type=int)
    hierarchy_match.add_argument("--radius", type=float, default=30.0)
    hierarchy_candidates = _add_parser(hierarchy_commands, "candidates", "List hierarchy match candidates.", "Prints one JSON row per candidate, ordered for review by catalog record and score. Candidate rows explain whether the evidence came from SIMBAD-style identifiers, position, or both.")
    hierarchy_candidates.add_argument("--provider", choices=["wds", "ccdm"])

    hierarchy_source = _add_parser(hierarchy_commands, "source", "Manage imported hierarchy snapshots.", "Fetch/import full WDS or CCDM catalog snapshots, list stored sources, and prune duplicates.")
    hierarchy_source_commands = hierarchy_source.add_subparsers(dest="source_command", required=True)
    hierarchy_source_fetch = _add_parser(hierarchy_source_commands, "fetch", "Fetch WDS or CCDM into hierarchy records.", "By default downloads or reuses the cached full VizieR catalog (WDS B/wds or CCDM I/274). With --file, loads a local full-catalog snapshot instead (fixed-width WDS-like rows and delimited files with recognizable column names are supported). Either way stores source/version metadata; matching to targets remains a separate review step.")
    hierarchy_source_fetch.add_argument("provider", choices=["wds", "ccdm"])
    hierarchy_source_fetch.add_argument("--file", help="load a local snapshot file instead of fetching from VizieR (requires --release)")
    hierarchy_source_fetch.add_argument("--release")
    hierarchy_source_fetch.add_argument("--note")
    hierarchy_source_fetch.add_argument("--refresh-cache", action="store_true", help="force a fresh VizieR download and replace the current cache entry")
    hierarchy_source_fetch.add_argument("--no-cache", action="store_true", help="download directly without reading or writing the snapshot cache")
    hierarchy_source_list = _add_parser(hierarchy_source_commands, "list", "List imported hierarchy source snapshots.", "Shows WDS, CCDM, SIMBAD-derived, or manual hierarchy source snapshots currently stored in the main database.")
    hierarchy_source_list.add_argument("--provider", choices=["wds", "ccdm", "simbad", "manual"])
    hierarchy_source_prune = _add_parser(hierarchy_source_commands, "prune", "Remove duplicate imported hierarchy snapshots.", "Keeps the earliest source for each provider/checksum pair and deletes duplicate source copies plus their derived candidates and graph edges. Use this after interrupted or repeated rehearsal imports.")
    hierarchy_source_prune.add_argument("--provider", choices=["wds", "ccdm", "simbad", "manual"])

    hierarchy_graph = _add_parser(hierarchy_commands, "graph", "Derive, list, diagnose, and override provider graph edges.", "The WDS/CCDM component graph is a reviewable structural layer kept separate from target-level relationships.")
    hierarchy_graph_commands = hierarchy_graph.add_subparsers(dest="graph_command", required=True)
    hierarchy_graph_derive = _add_parser(hierarchy_graph_commands, "derive", "Derive provider component graph edges.", "Builds reviewable hierarchy graph edges from imported provider rows. For WDS this normalizes pair labels such as AB, Aa,Ab, and AB,C while keeping the result separate from target-level relationships.")
    hierarchy_graph_derive.add_argument("provider", choices=["wds"])
    hierarchy_graph_derive.add_argument("--source-id", type=int)
    hierarchy_graph_list = _add_parser(hierarchy_graph_commands, "list", "List derived hierarchy graph edges.", "Shows effective provider graph edges after applying append-only manual overrides. The argument may be a WDS native ID or, with --target, an SDB target reference.")
    hierarchy_graph_list.add_argument("reference")
    hierarchy_graph_list.add_argument("--provider", choices=["wds"])
    hierarchy_graph_list.add_argument("--source-id", type=int)
    hierarchy_graph_list.add_argument("--target", action="store_true", help="interpret reference as a target rather than a provider native id")
    hierarchy_graph_diagnostics = _add_parser(hierarchy_graph_commands, "diagnostics", "List hierarchy graph diagnostics.", "Groups effective graph edges by provider native ID and reports review-level issues such as missing structural edges, duplicate parents, and structural edges without usable geometry. Disconnected but usable structural groups, such as AB plus CD, are retained as informational diagnostics.")
    hierarchy_graph_diagnostics.add_argument("--provider", choices=["wds"])
    hierarchy_graph_diagnostics.add_argument("--source-id", type=int)
    hierarchy_graph_diagnostics.add_argument("--limit", type=int, default=100, help="maximum rows to print; 0 prints all rows")
    hierarchy_graph_diagnostics.add_argument("--severity", choices=["review", "info"])
    hierarchy_graph_diagnostics.add_argument("--issue")
    hierarchy_graph_diagnostics.add_argument("--summary", action="store_true", help="print counts grouped by severity and issue")
    hierarchy_graph_override = _add_parser(hierarchy_graph_commands, "override", "Override one derived hierarchy graph edge.", "Adds an append-only audit record changing the effective status or relation type of a provider graph edge. This does not edit imported provider rows.")
    hierarchy_graph_override.add_argument("provider", choices=["wds"])
    hierarchy_graph_override.add_argument("native_id")
    hierarchy_graph_override.add_argument("--from", required=True, dest="reference_label")
    hierarchy_graph_override.add_argument("--to", required=True, dest="component_label")
    hierarchy_graph_override.add_argument("--source-id", type=int)
    hierarchy_graph_override.add_argument("--status")
    hierarchy_graph_override.add_argument("--type", dest="relation_type")
    hierarchy_graph_override.add_argument("--role", choices=["structural", "non_structural"], dest="structural_role")
    _add_actor_argument(hierarchy_graph_override)
    _add_reason_argument(hierarchy_graph_override)
    hierarchy_accept = _add_parser(hierarchy_commands, "accept-candidate", "Accept a hierarchy match candidate.", "Marks one WDS/CCDM candidate as accepted, writes an audit action, and creates a source-backed relationship evidence row. If --system is supplied, the target is also added to that system.")
    hierarchy_accept.add_argument("candidate_id", type=int)
    _add_actor_argument(hierarchy_accept)
    hierarchy_accept.add_argument("--reason", default="")
    hierarchy_accept.add_argument("--system")
    hierarchy_accept.add_argument("--component")
    hierarchy_accept.add_argument("--type", default="hierarchy_record", dest="relationship_type")
    hierarchy_reject = _add_parser(hierarchy_commands, "reject-candidate", "Reject a hierarchy match candidate.", "Marks one candidate as rejected and appends an audit action. Rejection does not delete provider rows or candidate evidence.")
    hierarchy_reject.add_argument("candidate_id", type=int)
    _add_actor_argument(hierarchy_reject)
    _add_reason_argument(hierarchy_reject)
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
    photometry = _add_parser(commands, "photometry", "Review and override normalized photometry inclusion.", "Use this to exclude or re-include specific band/provider measurements while preserving the raw provider row. Overrides are audited and affect future exports.")
    photometry_commands = photometry.add_subparsers(dest="photometry_command", required=True)
    for action in ("exclude", "include"):
        command = _add_parser(photometry_commands, action, f"{action.title()} one normalized photometry measurement.", "This records an audited action for one canonical measurement without deleting provider data. Excluded measurements remain inspectable and can be included again later.")
        command.add_argument("measurement_id", type=int)
        _add_actor_argument(command)
        _add_reason_argument(command)
    photometry_list = _add_parser(photometry_commands, "overrides", "List photometry inclusion/exclusion actions for a target.", "Shows the append-only include/exclude decisions recorded for a target's measurements, with actor and reason. It does not list the measurements themselves; use `photometry review` for association context.")
    photometry_list.add_argument("target")
    photometry_review = _add_parser(photometry_commands, "review", "Review current photometry association context for a target.", "Lists current normalized measurements plus unaccepted current raw catalog rows. This is read-only and intended to precede assign (ownership) and include/exclude (fit eligibility) decisions.")
    photometry_review.add_argument("target")
    photometry_queue = _add_parser(photometry_commands, "review-queue", "Prioritize photometry association rows needing review.", "Combines current photometry, unaccepted catalog neighbours, and hierarchy/resolution predictions. Select exactly one target set with TARGET, --sample, or --all.")
    photometry_queue.add_argument("target", nargs="?")
    photometry_queue.add_argument("--sample")
    photometry_queue.add_argument("--all", action="store_true", dest="review_all")
    photometry_queue.add_argument("--provider")
    photometry_queue.add_argument("--format", choices=["table", "json", "jsonl"], default="table")
    photometry_html = _add_parser(
        photometry_commands,
        "review-html",
        "Write a static photometry review HTML bundle.",
        "Generates one interactive review page for every selected target and "
        "an index page summarizing photometry review-queue signals. This is "
        "static output suitable for sample review or linking from exported products.",
    )
    photometry_html.add_argument("target", nargs="?")
    photometry_html.add_argument("--sample")
    photometry_html.add_argument("--all", action="store_true", dest="review_all")
    photometry_html.add_argument("--provider")
    photometry_html.add_argument("--output-dir", required=True)
    photometry_html.add_argument("--radius", type=float)
    photometry_html.add_argument(
        "--workers", type=int, default=min(4, os.cpu_count() or 1),
        help="worker processes used to render independent target pages",
    )
    photometry_html.add_argument("--open", action="store_true", help="open index.html in the default browser")
    photometry_assign = _add_parser(photometry_commands, "assign", "Assign one measurement to a contributing target or composite scope.", "Creates or updates the current many-to-many assignment and appends an audit action. This records ownership for future joint fitting but does not yet change legacy exports.")
    photometry_assign.add_argument("measurement_id", type=int)
    photometry_assign.add_argument("target")
    photometry_assign.add_argument(
        "--role",
        choices=MeasurementTargetRole.choices(),
        default=MeasurementTargetRole.CONTRIBUTOR.value,
    )
    photometry_assign.add_argument("--method", default="manual")
    photometry_assign.add_argument("--weight", type=float)
    _add_actor_argument(photometry_assign)
    _add_reason_argument(photometry_assign)
    photometry_unassign = _add_parser(photometry_commands, "unassign", "Remove a current measurement assignment while preserving its history.", "Deletes only the materialized current assignment and appends an unassign action. Provider rows, normalized photometry, and earlier assignment actions remain intact.")
    photometry_unassign.add_argument("measurement_id", type=int)
    photometry_unassign.add_argument("target")
    photometry_unassign.add_argument(
        "--role",
        choices=MeasurementTargetRole.choices(),
        default=MeasurementTargetRole.CONTRIBUTOR.value,
    )
    _add_actor_argument(photometry_unassign)
    _add_reason_argument(photometry_unassign)
    photometry_assignment_history = _add_parser(photometry_commands, "assignment-history", "List append-only measurement assignment actions.", "Shows every assign and unassign action for a target, including actor, reason, method, role, and optional response weight.")
    photometry_assignment_history.add_argument("target")
    photometry_proposals = _add_parser(photometry_commands, "proposals", "Propose system-level measurement contributors without changing the database.", "Uses exact identifiers, catalog positions, per-band resolution, hierarchy semantics, and target lifecycle state. Ambiguous rows remain review-required; use photometry assign separately to accept a proposal.")
    photometry_proposals.add_argument("target")
    photometry_proposals.add_argument(
        "--details", action="store_true",
        help="include per-measurement proposals; the default is summary-first",
    )
    photometry_apply_proposals = _add_parser(
        photometry_commands,
        "apply-proposals",
        "Preview or apply conservative high-confidence photometry assignments.",
        "Dry-run is the default and reports what would change for one target system or sample. "
        "Use --apply with --actor to persist only missing high-confidence assignments; existing "
        "conflicts and uncertain proposals remain untouched. Provider-excluded measurements may "
        "be assigned, but retain their exclusion until an audited photometry include override.",
    )
    photometry_apply_proposals.add_argument("target", nargs="?")
    photometry_apply_proposals.add_argument("--sample")
    photometry_apply_proposals.add_argument(
        "--apply", action="store_true",
        help="persist eligible assignments; without this flag the command is read-only",
    )
    photometry_apply_proposals.add_argument(
        "--actor", help="audit actor; required when --apply is used",
    )
    photometry_apply_proposals.add_argument(
        "--reason",
        default="accepted high-confidence automatic assignment proposal",
        help="audit reason prefix used for newly applied assignments",
    )
    photometry_apply_proposals.add_argument(
        "--details", action="store_true",
        help="include per-measurement results; the default is summary-first",
    )
    photometry_fitting_groups = _add_parser(
        photometry_commands,
        "fitting-groups",
        "Read-only joint-fitting groups and assignment views from accepted assignments.",
        "Physical targets are connected only by included measurements assigned to more than "
        "one contributor. Composite targets remain measurement scopes rather than model nodes; "
        "excluded and unresolved measurements are reported without changing the database. "
        "--view full (default) prints the whole projection; --view readiness summarizes "
        "system-level blockers and previews SIMBAD stellar relatives; --view assignments lists "
        "the current contributor/composite-scope projection for one target.",
    )
    photometry_fitting_groups.add_argument("target", nargs="?")
    photometry_fitting_groups.add_argument("--sample")
    photometry_fitting_groups.add_argument(
        "--view", choices=["full", "readiness", "assignments"], default="full",
    )
    photometry_fitting_groups.add_argument(
        "--format", choices=["table", "json", "jsonl"], default="table",
        help="readiness view only; full and assignments views always print JSON",
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


def _photometry_review_priority_rank(priority: str) -> int:
    return review_priority_rank(priority)


def _review_page_filename(sdbid: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in {"-", "_", ".", "+"} else "_"
        for char in sdbid
    )
    return f"{safe}.html"


def _write_review_page_task(
    task: tuple[str, str, float | None, str]
) -> tuple[str, str, str]:
    """Render one independent review page in a worker process."""

    database, reference, radius_arcsec, output_path = task
    from .review_sky_render import write_review_sky_html
    from .review_widget import build_review_sky_view

    sessions = make_session_factory(database)
    try:
        view = build_review_sky_view(
            sessions,
            reference,
            radius_arcsec=radius_arcsec,
        )
        output = write_review_sky_html(view, output_path)
        return view.sdbid, Path(output).name, str(Path(output).resolve())
    finally:
        bind = sessions.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _render_photometry_review_index(
    *,
    title: str,
    targets: list[str],
    queue_rows: list[dict[str, object]],
    page_by_sdbid: dict[str, str],
) -> str:
    import html

    grouped: dict[str, list[dict[str, object]]] = {sdbid: [] for sdbid in targets}
    for row in queue_rows:
        grouped.setdefault(str(row.get("sdbid") or ""), []).append(row)

    def esc(value) -> str:
        return html.escape("" if value is None else str(value), quote=True)

    def priority_for(rows: list[dict[str, object]]) -> str:
        if not rows:
            return "none"
        return max(
            (str(row.get("priority") or "none") for row in rows),
            key=_photometry_review_priority_rank,
        )

    table_rows: list[str] = []
    for sdbid in targets:
        rows = grouped.get(sdbid, [])
        priority = priority_for(rows)
        signal_count = sum(1 for row in rows if row.get("priority") != "none")
        signals = sorted({str(row.get("signal") or "") for row in rows if row.get("signal")})
        providers = sorted({str(row.get("provider") or "") for row in rows if row.get("provider")})
        actions = sorted({
            str(row.get("action") or "")
            for row in rows
            if row.get("action") and row.get("action") != "none"
        })
        table_rows.append(f"""
          <tr class="priority-{esc(priority)}">
            <td><a href="{esc(page_by_sdbid[sdbid])}"><code>{esc(sdbid)}</code></a></td>
            <td>{esc(priority)}</td>
            <td>{signal_count}</td>
            <td>{esc(", ".join(providers))}</td>
            <td>{esc("; ".join(signals))}</td>
            <td>{esc("; ".join(actions))}</td>
          </tr>
        """)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{esc(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #111827; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 7px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f8fafc; position: sticky; top: 0; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .muted {{ color: #64748b; }}
    .priority-high td, .priority-highest td {{ background: #fff7ed; }}
    .priority-medium td {{ background: #fefce8; }}
    .priority-low td {{ background: #f8fafc; }}
    .priority-none td {{ color: #64748b; }}
  </style>
</head>
<body>
  <h1>{esc(title)}</h1>
  <p class="muted">Static review bundle. Individual target pages show full sky/context views; this index highlights photometry review queue signals only.</p>
  <table>
    <thead><tr><th>target</th><th>priority</th><th>signals</th><th>providers</th><th>signal summary</th><th>suggested action</th></tr></thead>
    <tbody>{''.join(table_rows)}</tbody>
  </table>
</body>
</html>
"""


def _format_photometry_review_queue_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No photometry review queue rows."
    headers = (
        "priority", "sdbid", "provider", "band", "signal",
        "predicted", "action",
    )
    rendered = ["  ".join(headers)]
    for row in rows:
        predicted = row.get("predicted_scope") or row.get("predicted_blend_state") or ""
        rendered.append("  ".join([
            str(row.get("priority") or ""),
            str(row.get("sdbid") or ""),
            str(row.get("provider") or ""),
            str(row.get("band") or ""),
            str(row.get("signal") or ""),
            str(predicted),
            str(row.get("action") or ""),
        ]))
    return "\n".join(rendered)


def _format_assignment_readiness_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No system-level assignment blockers."
    columns = (
        ("priority", "priority"),
        ("sdbid", "sdbid"),
        ("role", "role"),
        ("measurement_count", "meas"),
        ("included_measurement_count", "incl"),
        ("detection_count", "det"),
        ("provider_bands", "providers/bands"),
        ("imported_physical_count", "physical"),
        ("importable_relative_count", "import"),
        ("recommended_action", "action"),
    )
    values = []
    for row in rows:
        display = dict(row)
        display["provider_bands"] = ";".join(
            f"{provider['provider']}:{','.join(provider['bands'])}"
            for provider in row["providers"]
        )
        display["imported_physical_count"] = len(
            row["imported_physical_relatives"]
        )
        values.append([
            str(display.get(key, "")) for key, _heading in columns
        ])
    headings = [heading for _key, heading in columns]
    widths = [
        max(len(heading), *(len(row[index]) for row in values))
        for index, heading in enumerate(headings)
    ]
    lines = [
        "  ".join(heading.ljust(widths[index]) for index, heading in enumerate(headings)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in values
    )
    return "\n".join(lines)


def _format_hierarchy_photometry_review_table(rows: list[dict[str, object]]) -> str:
    columns = [
        ("sdbid", "sdbid"),
        ("classification", "class"),
        ("component_assignment_status", "assignment"),
        ("hierarchy_decision_basis", "basis"),
        ("target_level", "level"),
        ("nearest_pair_arcsec", "sep(\")"),
        ("measurement_count", "n"),
        ("predicted_scope_counts", "scope"),
        ("likely_blended_bands", "blended bands"),
        ("recommendation", "recommendation"),
    ]
    table_rows = []
    for row in rows:
        table_rows.append([
            _format_table_value(key, row.get(key))
            for key, _heading in columns
        ])
    if not table_rows:
        return "No matching photometry review rows."
    headings = [heading for _key, heading in columns]
    widths = [
        max(len(heading), *(len(row[index]) for row in table_rows))
        for index, heading in enumerate(headings)
    ]
    lines = [
        "  ".join(heading.ljust(widths[index]) for index, heading in enumerate(headings)),
        "  ".join("-" * width for width in widths),
    ]
    for row in table_rows:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def _format_hierarchy_review_queue_table(rows: list[dict[str, object]]) -> str:
    columns = [
        ("priority", "priority"),
        ("sdbid", "sdbid"),
        ("basis", "basis"),
        ("candidate_count", "cand"),
        ("accepted_count", "acc"),
        ("likely_blended_bands", "blended bands"),
        ("nearest_pair_arcsec", "sep(\")"),
        ("reason", "reason"),
    ]
    table_rows = []
    for row in rows:
        table_rows.append([
            _format_table_value(key, row.get(key))
            for key, _heading in columns
        ])
    if not table_rows:
        return "No matching hierarchy review queue rows."
    headings = [heading for _key, heading in columns]
    widths = [
        max(len(heading), *(len(row[index]) for row in table_rows))
        for index, heading in enumerate(headings)
    ]
    lines = [
        "  ".join(heading.ljust(widths[index]) for index, heading in enumerate(headings)),
        "  ".join("-" * width for width in widths),
    ]
    for row in table_rows:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def _format_table_value(key: str, value) -> str:
    if value is None:
        return "-"
    if key == "nearest_pair_arcsec":
        return f"{float(value):.2f}"
    if key == "likely_blended_bands":
        values = list(value or [])
        if not values:
            return "-"
        return ",".join(str(item).split(":", 1)[-1] for item in values)
    if key in {"predicted_scope_counts", "predicted_blend_counts"}:
        values = dict(value or {})
        if not values:
            return "-"
        return ",".join(f"{name}:{count}" for name, count in sorted(values.items()))
    return str(value)


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
        from .hierarchy import HierarchyService

        service = HierarchyService(sessions)
        try:
            if args.hierarchy_command == "create-system":
                value = service.create_system(
                    args.name,
                    primary=args.primary,
                    source=args.source,
                    note=args.note,
                )
                print(_format_json(args, {
                    "system_id": value.id,
                    "name": value.name,
                    "primary_target_id": value.primary_target_id,
                    "source": value.source,
                }, sort_keys=True))
            elif args.hierarchy_command == "add-member":
                value = service.add_member(
                    args.system,
                    args.target,
                    component_label=args.component,
                    source=args.source,
                )
                print(_format_json(args, {
                    "member_id": value.id,
                    "system_id": value.system_id,
                    "target_id": value.target_id,
                    "component_label": value.component_label,
                    "source": value.source,
                }, sort_keys=True))
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
                if value.direction == "a_parent_b":
                    parent_id, child_id = value.endpoint_a_target_id, value.endpoint_b_target_id
                    primary_id = secondary_id = None
                elif value.direction == "b_parent_a":
                    parent_id, child_id = value.endpoint_b_target_id, value.endpoint_a_target_id
                    primary_id = secondary_id = None
                else:
                    primary_id, secondary_id = value.endpoint_a_target_id, value.endpoint_b_target_id
                    parent_id = child_id = None
                print(_format_json(args, {
                    "relationship_id": value.id,
                    "relationship_type": value.relation_type,
                    "system_id": value.system_id,
                    "parent_target_id": parent_id,
                    "child_target_id": child_id,
                    "primary_target_id": primary_id,
                    "secondary_target_id": secondary_id,
                    "source": value.source,
                    "status": value.status,
                }, sort_keys=True))
            elif args.hierarchy_command == "relatives":
                from .system_expansion import preview_immediate_relatives

                print(_format_json(
                    args,
                    preview_immediate_relatives(sessions, args.target),
                    sort_keys=True,
                ))
            elif args.hierarchy_command == "import-relatives":
                if args.offline:
                    raise ValueError("hierarchy import-relatives is unavailable in offline mode")
                with _provider_output_to_stderr():
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
                print(_format_json(args, value.as_dict(), sort_keys=True))
            elif args.hierarchy_command == "source":
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
                        with _provider_output_to_stderr():
                            value = service.fetch_snapshot(
                                args.provider,
                                cache_path=None if args.no_cache else args.cache_database,
                                refresh_cache=args.refresh_cache,
                                release=args.release,
                                note=args.note,
                            )
                    print(_format_json(args, asdict(value), sort_keys=True))
                elif args.source_command == "list":
                    for value in service.sources(args.provider):
                        print(_format_json(args, {
                            "source_id": value.id,
                            "provider": value.provider,
                            "release": value.release,
                            "source_file": value.source_file,
                            "checksum": value.checksum,
                            "fetched_at": None if value.fetched_at is None else value.fetched_at.isoformat(),
                            "imported_at": value.imported_at.isoformat(),
                            "note": value.note,
                        }, sort_keys=True))
                else:  # prune
                    value = service.prune_duplicate_sources(args.provider)
                    print(_format_json(args, asdict(value), sort_keys=True))
            elif args.hierarchy_command == "summary":
                print(_format_json(args, service.summary(args.provider, source_id=args.source_id), sort_keys=True))
            elif args.hierarchy_command == "match":
                value = service.match_records(
                    args.provider,
                    source_id=args.source_id,
                    radius_arcsec=args.radius,
                )
                print(_format_json(args, asdict(value), sort_keys=True))
            elif args.hierarchy_command == "candidates":
                for value in service.review_matches(args.provider):
                    print(_format_json(args, asdict(value), sort_keys=True))
            elif args.hierarchy_command == "review-queue":
                selectors = sum((
                    args.target is not None,
                    args.sample is not None,
                    args.review_all,
                ))
                if selectors != 1:
                    raise ValueError("provide exactly one of TARGET, --sample, or --all")
                if args.review_all:
                    with sessions() as session:
                        references = list(session.scalars(
                            select(Target.sdbid).order_by(Target.sdbid)
                        ))
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
                    table_formatter = _format_hierarchy_photometry_review_table
                else:
                    rows = service.review_queue(
                        references,
                        provider=args.provider,
                        min_priority=args.min_priority,
                    )
                    table_formatter = _format_hierarchy_review_queue_table
                if args.format == "table":
                    print(table_formatter(rows))
                elif args.format == "json":
                    print(_format_json(args, rows, sort_keys=True))
                else:
                    for value in rows:
                        print(_format_json(args, value, sort_keys=True))
            elif args.hierarchy_command == "graph":
                if args.graph_command == "derive":
                    value = service.derive_graph(
                        args.provider,
                        source_id=args.source_id,
                    )
                    print(_format_json(args, asdict(value), sort_keys=True))
                elif args.graph_command == "list":
                    rows = service.graph_edges(
                        provider=args.provider,
                        native_id=None if args.target else args.reference,
                        target=args.reference if args.target else None,
                        source_id=args.source_id,
                    )
                    for value in rows:
                        print(_format_json(args, asdict(value), sort_keys=True))
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
                            key=lambda item: (0 if item[0][0] == "review" else 1, item[0][1]),
                        ):
                            print(_format_json(args, {
                                "count": count,
                                "issue": issue,
                                "severity": severity,
                            }, sort_keys=True))
                    else:
                        for value in rows:
                            print(_format_json(args, asdict(value), sort_keys=True))
                else:  # override
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
                    print(_format_json(args, asdict(value), sort_keys=True))
            elif args.hierarchy_command == "accept-candidate":
                value = service.accept_match(
                    args.candidate_id,
                    actor=args.actor,
                    reason=args.reason,
                    system=args.system,
                    component_label=args.component,
                    relationship_type=args.relationship_type,
                )
                print(_format_json(args, asdict(value), sort_keys=True))
            elif args.hierarchy_command == "reject-candidate":
                value = service.reject_match(
                    args.candidate_id,
                    actor=args.actor,
                    reason=args.reason,
                )
                print(_format_json(args, asdict(value), sort_keys=True))
            elif args.hierarchy_command == "target-state":
                from .target_lifecycle import target_lifecycle_status

                print(_format_json(
                    args,
                    asdict(target_lifecycle_status(sessions, args.target)),
                    sort_keys=True,
                ))
            elif args.hierarchy_command == "set-target-state":
                from .target_lifecycle import set_target_lifecycle, target_lifecycle_status

                set_target_lifecycle(
                    sessions,
                    args.target,
                    role=args.role,
                    state=args.state,
                    superseded_by=args.superseded_by,
                    actor=args.actor,
                    reason=args.reason,
                )
                print(_format_json(
                    args,
                    asdict(target_lifecycle_status(sessions, args.target)),
                    sort_keys=True,
                ))
            else:  # status
                if args.scope == "provider":
                    print(_format_json(args, service.target_context(args.target), sort_keys=True))
                elif args.scope == "system":
                    print(_format_json(args, service.system_context(args.target), sort_keys=True))
                else:
                    print(_format_json(args, service.status(args.target).as_dict(), sort_keys=True))
        except (KeyError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
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
        from .photometry import (
            assign_measurement_target,
            list_measurement_assignment_history,
            list_measurement_target_assignments,
            list_measurement_eligibility_actions,
            review_photometry_associations,
            set_measurement_eligibility,
            unassign_measurement_target,
        )

        try:
            if args.photometry_command in {"exclude", "include"}:
                value = set_measurement_eligibility(
                    sessions,
                    args.measurement_id,
                    excluded=args.photometry_command == "exclude",
                    actor=args.actor,
                    reason=args.reason,
                )
                values = [value]
                for value in values:
                    print(_format_json(args, {
                        "id": value.id,
                        "measurement_id": value.measurement_id,
                        "excluded": value.excluded,
                        "actor": value.actor,
                        "reason": value.reason,
                        "created_at": value.created_at.isoformat(),
                    }, sort_keys=True))
            elif args.photometry_command == "overrides":
                for value in list_measurement_eligibility_actions(
                    sessions, args.target,
                ):
                    print(_format_json(args, {
                        "id": value.id,
                        "measurement_id": value.measurement_id,
                        "excluded": value.excluded,
                        "actor": value.actor,
                        "reason": value.reason,
                        "created_at": value.created_at.isoformat(),
                    }, sort_keys=True))
            elif args.photometry_command == "review":
                print(_format_json(
                    args, [asdict(value) for value in review_photometry_associations(sessions, args.target)],
                    sort_keys=True,
                ))
            elif args.photometry_command == "review-queue":
                from .photometry import photometry_review_queue
                selectors = sum((
                    args.target is not None,
                    args.sample is not None,
                    args.review_all,
                ))
                if selectors != 1:
                    raise ValueError("provide exactly one of TARGET, --sample, or --all")
                if args.review_all:
                    with sessions() as session:
                        references = list(session.scalars(
                            select(Target.sdbid).order_by(Target.sdbid)
                        ))
                elif args.sample is not None:
                    from .samples import SampleService

                    references = [
                        target.sdbid
                        for target in SampleService(sessions).members(args.sample)
                    ]
                else:
                    references = [args.target]
                values = photometry_review_queue(
                    sessions, references, provider=args.provider,
                )
                if args.format == "table":
                    print(_format_photometry_review_queue_table(values))
                elif args.format == "json":
                    print(_format_json(args, values, sort_keys=True))
                else:
                    for value in values:
                        print(_format_json(args, value, sort_keys=True))
            elif args.photometry_command == "review-html":
                from .photometry import photometry_review_queue
                from .review_sky_render import write_review_sky_html
                from .review_widget import build_review_sky_view

                selectors = sum((
                    args.target is not None,
                    args.sample is not None,
                    args.review_all,
                ))
                if selectors != 1:
                    raise ValueError("provide exactly one of TARGET, --sample, or --all")
                if args.review_all:
                    with sessions() as session:
                        references = list(session.scalars(
                            select(Target.sdbid).order_by(Target.sdbid)
                        ))
                elif args.sample is not None:
                    from .samples import SampleService

                    references = [
                        target.sdbid
                        for target in SampleService(sessions).members(args.sample)
                    ]
                else:
                    references = [args.target]
                if args.workers < 1:
                    raise ValueError("--workers must be at least 1")

                output_dir = Path(args.output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                page_by_sdbid: dict[str, str] = {}
                target_sdbids: list[str] = []
                review_pages: list[str] = []
                tasks = [
                    (
                        str(path.resolve()),
                        str(reference),
                        args.radius,
                        str(output_dir / _review_page_filename(str(reference))),
                    )
                    for reference in references
                ]
                if args.workers == 1 or len(tasks) == 1:
                    rendered = map(_write_review_page_task, tasks)
                    for sdbid, filename, output_path in rendered:
                        page_by_sdbid[sdbid] = filename
                        target_sdbids.append(sdbid)
                        review_pages.append(output_path)
                else:
                    worker_count = min(args.workers, len(tasks))
                    with ProcessPoolExecutor(max_workers=worker_count) as executor:
                        for sdbid, filename, output_path in executor.map(
                            _write_review_page_task, tasks
                        ):
                            page_by_sdbid[sdbid] = filename
                            target_sdbids.append(sdbid)
                            review_pages.append(output_path)

                values = photometry_review_queue(
                    sessions, target_sdbids, provider=args.provider,
                )
                index_path = output_dir / "index.html"
                index_path.write_text(_render_photometry_review_index(
                    title="SDB photometry review",
                    targets=target_sdbids,
                    queue_rows=values,
                    page_by_sdbid=page_by_sdbid,
                ))
                if args.open:
                    import webbrowser

                    webbrowser.open(index_path.resolve().as_uri())
                print(_format_json(args, {
                    "output_dir": str(output_dir.resolve()),
                    "index": str(index_path.resolve()),
                    "targets": len(target_sdbids),
                    "review_pages": review_pages,
                    "queue_rows": len(values),
                    "signal_rows": sum(
                        1 for value in values if value.get("priority") != "none"
                    ),
                }, sort_keys=True))
            elif args.photometry_command == "assign":
                value = assign_measurement_target(
                    sessions,
                    args.measurement_id,
                    args.target,
                    role=args.role,
                    method=args.method,
                    weight=args.weight,
                    actor=args.actor,
                    reason=args.reason,
                )
                print(_format_json(args, {
                    "association_id": value.id,
                    "measurement_id": value.measurement_id,
                    "target_id": value.target_id,
                    "role": value.role,
                    "method": value.method,
                    "weight": value.weight,
                    "note": value.note,
                }, sort_keys=True))
            elif args.photometry_command == "unassign":
                value = unassign_measurement_target(
                    sessions,
                    args.measurement_id,
                    args.target,
                    role=args.role,
                    actor=args.actor,
                    reason=args.reason,
                )
                print(_format_json(args, {
                    "action_id": value.id,
                    "measurement_id": value.measurement_id,
                    "target_id": value.target_id,
                    "action": value.action,
                    "role": value.role,
                    "actor": value.actor,
                    "reason": value.reason,
                }, sort_keys=True))
            elif args.photometry_command == "assignment-history":
                print(_format_json(args, [{
                    "action_id": value.id,
                    "measurement_id": value.measurement_id,
                    "target_id": value.target_id,
                    "action": value.action,
                    "role": value.role,
                    "method": value.method,
                    "weight": value.weight,
                    "actor": value.actor,
                    "reason": value.reason,
                    "created_at": value.created_at.isoformat(),
                } for value in list_measurement_assignment_history(
                    sessions, args.target,
                )], sort_keys=True))
            elif args.photometry_command == "proposals":
                from .assignment_proposals import measurement_assignment_proposals
                from .proposal_reporting import proposal_summary_report

                print(_format_json(
                    args,
                    proposal_summary_report(
                        measurement_assignment_proposals(sessions, args.target),
                        target=args.target,
                        include_details=args.details,
                    ),
                    sort_keys=True,
                ))
            elif args.photometry_command == "apply-proposals":
                from .proposal_application import apply_measurement_assignment_proposals
                from .proposal_reporting import without_proposal_items

                print(_format_json(
                    args,
                    without_proposal_items(
                        apply_measurement_assignment_proposals(
                            sessions,
                            target_reference=args.target,
                            sample=args.sample,
                            apply=args.apply,
                            actor=args.actor,
                            reason=args.reason,
                            reporter=reporter,
                        ),
                        include_details=args.details,
                    ),
                    sort_keys=True,
                ))
            elif args.photometry_command == "fitting-groups":
                if args.view == "assignments":
                    print(_format_json(
                        args,
                        list_measurement_target_assignments(sessions, args.target),
                        sort_keys=True,
                    ))
                elif args.view == "readiness":
                    from .assignment_readiness import assignment_readiness_report

                    report = assignment_readiness_report(
                        sessions,
                        target_reference=args.target,
                        sample=args.sample,
                    )
                    if args.format == "table":
                        print(_format_assignment_readiness_table(report["rows"]))
                    elif args.format == "jsonl":
                        for value in report["rows"]:
                            print(_format_json(args, value, sort_keys=True))
                    else:
                        print(_format_json(args, report, sort_keys=True))
                else:  # full
                    from .fitting_groups import fitting_group_report

                    print(_format_json(
                        args,
                        fitting_group_report(
                            sessions,
                            target_reference=args.target,
                            sample=args.sample,
                        ),
                        sort_keys=True,
                    ))
        except (KeyError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
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
