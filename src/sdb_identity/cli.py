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

from .database import init_database, make_session_factory
from .models import AstrometricSolution, CatalogRun, MatchCandidate, RawCatalogRow, Target
from .providers import ProviderError
from .service import AddRequest, IdentityService, UnresolvedTarget

REFERENCE_ADAPTERS = (
    "gaspar13", "v70a", "iras_psc", "iras_fsc", "hip2", "tdsc",
    "ubvmeans", "paunzen15", "koen10",
)


def _add_parser(subparsers, name: str, summary: str, detail: str, **kwargs):
    return subparsers.add_parser(
        name,
        help=summary,
        description=f"{summary}\n\n{detail}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        **kwargs,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="sdb",
        description=(
            "Manage the Python SDB identity, catalog, sample, and export "
            "database. Commands are designed to preserve provenance and record "
            "reviewable decisions rather than editing provider results in place."
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
    status = _add_parser(commands, "status", "Show the identity and provider state for one target.", "TARGET may be an sdbid or known identifier. The report is intended for quick inspection before refreshing catalogs, reviewing matches, or exporting photometry.")
    status.add_argument("target")
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
    override = _add_parser(commands, "override-match", "Manually accept an identity match candidate.", "This records an append-only audit decision and marks sibling identity candidates as not accepted. Use it only after inspecting `sdb review matches` output.")
    override.add_argument("candidate_id", type=int)
    override.add_argument("--actor", required=True)
    override.add_argument("--reason", required=True)
    catalog_override = _add_parser(commands, "override-catalog-match", "Manually accept a catalog photometry match candidate.", "This changes the current catalog association through an audited override rather than editing raw provider rows. Use it for ambiguous catalog matches after checking source IDs, separation, and notes.")
    catalog_override.add_argument("candidate_id", type=int)
    catalog_override.add_argument("--actor", required=True)
    catalog_override.add_argument("--reason", required=True)
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
    alma = _add_parser(commands, "alma", "Manage the local ALMA archive lookup cache.", "The ALMA cache stores compact project/member/pointing data used to find archive projects near a target. Syncing is separate from photometry updates and can be bootstrapped, resumed, or incrementally refreshed.")
    alma_commands = alma.add_subparsers(dest="alma_command", required=True)
    alma_sync = _add_parser(alma_commands, "sync", "Synchronise ALMA archive observations into the local cache.", "Use --bootstrap for a full archive load, --incremental for recent archive updates, or --resume to continue a previous sync run. The cache is used for target-nearby project links and is independent of photometry export.")
    alma_mode = alma_sync.add_mutually_exclusive_group(required=True)
    alma_mode.add_argument("--bootstrap", action="store_true")
    alma_mode.add_argument("--incremental", action="store_true")
    alma_mode.add_argument("--resume", type=int, metavar="RUN_ID")
    alma_sync.add_argument("--start-year", type=int, default=2011)
    alma_sync.add_argument("--end-year", type=int)
    alma_sync.add_argument("--chunk-months", type=int, default=3)
    alma_sync.add_argument("--archive-url")
    alma_sync.add_argument("--timeout", type=float, default=300)
    alma_status = _add_parser(alma_commands, "status", "Show recent ALMA sync and cache status.", "Reports recent sync runs and chunk progress so long-running archive updates can be monitored. Use --limit to control how much history is shown.")
    alma_status.add_argument("--limit", type=int, default=10)
    _add_parser(alma_commands, "compact", "Compact raw ALMA observations into member-level rows.", "Deduplicates archive observations by member OUS and prepares the smaller lookup tables used by target searches. Run this after sync if compacted state needs rebuilding.")
    _add_parser(alma_commands, "rebuild-bounds", "Rebuild ALMA member sky bounds.", "Recomputes cached spatial bounds used to speed project lookups. This is a maintenance command for cache repairs or schema changes.")
    _add_parser(alma_commands, "rebuild-positions", "Rebuild ALMA pointing positions.", "Recomputes the pointings used by target-radius lookups from cached archive rows. This is useful after cache repair or changes to position extraction.")
    alma_projects = _add_parser(alma_commands, "projects", "List ALMA projects near one target.", "Searches cached ALMA pointings near the target position and reports associated project/member information. It does not contact the ALMA archive.")
    alma_projects.add_argument("target")
    alma_projects.add_argument("--radius", type=float, default=10.0)
    cache = _add_parser(commands, "cache", "Inspect the generic provider snapshot cache.", "Shows cached raw-provider snapshots used by hierarchy and reference fetches. These commands are read-only and do not query remote services.")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_status = _add_parser(cache_commands, "status", "List cached provider snapshots.", "Reports cached snapshots by provider and catalog, including table counts, row counts, checksums, source URLs, and fetch times.")
    cache_status.add_argument("--all", action="store_true", dest="cache_all", help="include superseded cached snapshots")
    cache_tables = _add_parser(cache_commands, "tables", "List tables in one cached provider snapshot.", "Shows table names, row counts, descriptions, and column metadata for a cached provider catalog. Use --provider if the same catalog ID exists from more than one provider.")
    cache_tables.add_argument("catalog")
    cache_tables.add_argument("--provider")
    cache_readme = _add_parser(cache_commands, "readme", "Print the ReadMe for one cached provider snapshot.", "Reads the cached provider ReadMe without making a network request. Use --provider if the same catalog ID exists from more than one provider.")
    cache_readme.add_argument("catalog")
    cache_readme.add_argument("--provider")
    cache_validate = _add_parser(cache_commands, "validate", "Validate one cached provider snapshot.", "Checks that a current cached snapshot has source metadata, ReadMe text, tables, rows, and column metadata. If a matching reference snapshot exists, its interpreted row count is reported for comparison.")
    cache_validate.add_argument("catalog")
    cache_validate.add_argument("--provider")
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
    hierarchy_relatives = _add_parser(hierarchy_commands, "relatives", "Preview immediate SIMBAD relatives and whether they should become targets.", "Classifies current parent/child rows as already imported, importable stellar/substellar structure, contextual-only, or review-required. This is read-only and never follows relationships recursively.")
    hierarchy_relatives.add_argument("target")
    hierarchy_import_relatives = _add_parser(hierarchy_commands, "import-relatives", "Import immediate stellar SIMBAD relatives and reconcile one target system.", "Imports only current immediate stellar/substellar parents and children, then records component membership, lifecycle roles, and SIMBAD parent/child evidence. Clusters, moving groups, planets, disks, unknown types, and relatives of newly added targets are not expanded.")
    hierarchy_import_relatives.add_argument("target")
    hierarchy_import_relatives.add_argument("--actor", required=True)
    hierarchy_import_relatives.add_argument("--reason", required=True)
    hierarchy_target_state = _add_parser(hierarchy_commands, "target-state", "Show the current physical/composite role and lifecycle state.", "Existing targets default to unspecified/active. States are append-only review decisions and do not yet suppress or otherwise change legacy exports.")
    hierarchy_target_state.add_argument("target")
    hierarchy_set_target_state = _add_parser(hierarchy_commands, "set-target-state", "Record an audited target role and lifecycle state.", "Use physical for fitted stellar components and composite for scopes such as AB. Suppressed, archived, system-only, and superseded states are recorded now but do not alter export until an explicit policy is enabled.")
    hierarchy_set_target_state.add_argument("target")
    hierarchy_set_target_state.add_argument("--role", choices=["unspecified", "physical", "composite"], required=True)
    hierarchy_set_target_state.add_argument("--state", choices=["active", "system_only", "review_only", "suppressed", "superseded", "archived"], required=True)
    hierarchy_set_target_state.add_argument("--superseded-by")
    hierarchy_set_target_state.add_argument("--actor", required=True)
    hierarchy_set_target_state.add_argument("--reason", required=True)
    hierarchy_review_queue = _add_parser(hierarchy_commands, "review-queue", "Prioritize hierarchy targets needing review.", "Combines hierarchy candidates, accepted decisions, diagnostics, and photometry blend context for a target set. --view priority (default) ranks targets by review priority; --view blend lists hierarchy/blend photometry context per band. Select exactly one target set with TARGET, --sample, or --all.")
    hierarchy_review_queue.add_argument("target", nargs="?")
    hierarchy_review_queue.add_argument("--view", choices=["priority", "blend"], default="priority")
    hierarchy_review_queue.add_argument("--sample")
    hierarchy_review_queue.add_argument("--all", action="store_true", dest="review_all")
    hierarchy_review_queue.add_argument("--provider")
    hierarchy_review_queue.add_argument("--min-priority", choices=["none", "low", "medium", "high", "highest"], help="priority view only")
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
    hierarchy_graph_override.add_argument("--actor", required=True)
    hierarchy_graph_override.add_argument("--reason", required=True)
    hierarchy_accept = _add_parser(hierarchy_commands, "accept-candidate", "Accept a hierarchy match candidate.", "Marks one WDS/CCDM candidate as accepted, writes an audit action, and creates a source-backed relationship evidence row. If --system is supplied, the target is also added to that system.")
    hierarchy_accept.add_argument("candidate_id", type=int)
    hierarchy_accept.add_argument("--actor", required=True)
    hierarchy_accept.add_argument("--reason", default="")
    hierarchy_accept.add_argument("--system")
    hierarchy_accept.add_argument("--component")
    hierarchy_accept.add_argument("--type", default="hierarchy_record", dest="relationship_type")
    hierarchy_reject = _add_parser(hierarchy_commands, "reject-candidate", "Reject a hierarchy match candidate.", "Marks one candidate as rejected and appends an audit action. Rejection does not delete provider rows or candidate evidence.")
    hierarchy_reject.add_argument("candidate_id", type=int)
    hierarchy_reject.add_argument("--actor", required=True)
    hierarchy_reject.add_argument("--reason", required=True)
    attributes = _add_parser(commands, "attributes", "Show current catalog attributes for one target.", "Attributes are non-photometric catalog values such as ages or flags copied from provider rows. They are versioned like photometry and can be filtered by --key.")
    attributes.add_argument("target")
    attributes.add_argument("--key")
    note = _add_parser(commands, "note", "Add or list operator notes for targets.", "Notes are lightweight human annotations stored alongside the target without changing provider data. They are useful for decisions, caveats, and follow-up reminders.")
    note_commands = note.add_subparsers(dest="note_command", required=True)
    note_add = _add_parser(note_commands, "add", "Add an operator note to a target.", "Notes are append-only annotations for human context and do not alter provider rows or exported measurements directly. Include an actor so later reviews can trace who added the note.")
    note_add.add_argument("target")
    note_add.add_argument("text")
    note_add.add_argument("--actor", required=True)
    note_list = _add_parser(note_commands, "list", "List operator notes for a target.", "Shows notes in database order for quick review. Use this before making manual overrides when the target has known caveats.")
    note_list.add_argument("target")
    sample = _add_parser(commands, "sample", "Create, edit, inspect, and export target samples.", "Samples group arbitrary targets and keep small metadata such as date and note. Membership changes are audited and can drive readiness checks and sample exports.")
    sample_commands = sample.add_subparsers(dest="sample_command", required=True)
    sample_create = _add_parser(sample_commands, "create", "Create a named target sample.", "Samples group targets for update, readiness, and export workflows. Optional date and note fields capture lightweight provenance about the sample definition.")
    sample_create.add_argument("name")
    sample_create.add_argument("--date")
    sample_create.add_argument("--note")
    sample_set = _add_parser(sample_commands, "set", "Update sample metadata.", "Changes the sample date or note without changing membership. Membership additions and removals use separate audited commands.")
    sample_set.add_argument("name")
    sample_set.add_argument("--date")
    sample_set.add_argument("--note")
    _add_parser(sample_commands, "list", "List known samples.", "Shows sample names and stored metadata. Use this to discover sample names for update, readiness, or export commands.")
    for action in ("add", "remove"):
        sample_action = _add_parser(sample_commands, action, f"{action.title()} one target {'to' if action == 'add' else 'from'} a sample.", "Membership changes are audited with actor and reason. A target can belong to any number of samples.")
        sample_action.add_argument("name")
        sample_action.add_argument("target")
        sample_action.add_argument("--actor", required=True)
        sample_action.add_argument("--reason", required=True)
    sample_members = _add_parser(sample_commands, "members", "List current members of a sample.", "Outputs the targets currently assigned to the sample. This is the membership source used by sample readiness and sample export.")
    sample_members.add_argument("name")
    sample_readiness = _add_parser(sample_commands, "readiness", "Check whether a sample is ready for export or review.", "Reports missing, ambiguous, failed, and dirty provider state across all sample members. The command exits non-zero when blockers remain.")
    sample_readiness.add_argument("name")
    sample_readiness.add_argument(
        "--providers",
        default="simbad,gaia_dr3,tycho2,2mass,allwise",
        help="comma-separated providers expected for every sample member",
    )
    sample_import = _add_parser(sample_commands, "import", "Import sample membership from a file.", "Adds memberships in bulk while recording actor and reason. The target identities must already exist or be resolvable by the importer format.")
    sample_import.add_argument("name")
    sample_import.add_argument("file")
    sample_import.add_argument("--actor", required=True)
    sample_import.add_argument("--reason", required=True)
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
        command = _add_parser(photometry_commands, action, f"{action.title()} one normalized photometry measurement.", "This records an audited override for a band/provider pair without deleting raw provider data. Excluded measurements remain inspectable and can be included again later.")
        command.add_argument("target")
        command.add_argument("band")
        command.add_argument("--provider", required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--reason", required=True)
    photometry_list = _add_parser(photometry_commands, "overrides", "List photometry inclusion/exclusion overrides for a target.", "Shows the append-only include/exclude override decisions recorded for a target's measurements, with actor and reason. It does not list the measurements themselves; use `photometry review` for association context.")
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
    photometry_assign.add_argument("--role", choices=["contributor", "composite_scope"], default="contributor")
    photometry_assign.add_argument("--method", default="manual")
    photometry_assign.add_argument("--weight", type=float)
    photometry_assign.add_argument("--actor", required=True)
    photometry_assign.add_argument("--reason", required=True)
    photometry_unassign = _add_parser(photometry_commands, "unassign", "Remove a current measurement assignment while preserving its history.", "Deletes only the materialized current assignment and appends an unassign action. Provider rows, normalized photometry, and earlier assignment actions remain intact.")
    photometry_unassign.add_argument("measurement_id", type=int)
    photometry_unassign.add_argument("target")
    photometry_unassign.add_argument("--role", choices=["contributor", "composite_scope"], default="contributor")
    photometry_unassign.add_argument("--actor", required=True)
    photometry_unassign.add_argument("--reason", required=True)
    photometry_assignment_history = _add_parser(photometry_commands, "assignment-history", "List append-only measurement assignment actions.", "Shows every assign and unassign action for a target, including actor, reason, method, role, and optional response weight.")
    photometry_assignment_history.add_argument("target")
    photometry_proposals = _add_parser(photometry_commands, "proposals", "Propose system-level measurement contributors without changing the database.", "Uses exact identifiers, catalog positions, per-band resolution, hierarchy semantics, and target lifecycle state. Ambiguous rows remain review-required; use photometry assign separately to accept a proposal.")
    photometry_proposals.add_argument("target")
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
    dataset = _add_parser(commands, "dataset", "Import and manage curated source-controlled datasets.", "Curated datasets such as submm_obs are reimportable tables maintained outside remote providers. The commands reconcile records to targets, review unresolved rows, and control export inclusion.")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    dataset_import = _add_parser(dataset_commands, "import", "Import a curated dataset file.", "Loads source-controlled curated records such as submm_obs into versioned dataset tables. Reimports are expected as the file evolves through manual edits or pull requests.")
    dataset_import.add_argument("dataset", choices=["submm_obs"])
    dataset_import.add_argument("file")
    dataset_status = _add_parser(dataset_commands, "status", "Show curated dataset import and association status.", "Reports the latest import state and unresolved records. Use this after importing to see whether records need target association review.")
    dataset_status.add_argument("dataset", choices=["submm_obs"])
    dataset_review = _add_parser(dataset_commands, "review", "List unresolved curated dataset records.", "Shows records that could not be confidently associated with targets. These can be manually associated or left unresolved until more identity information is available.")
    dataset_review.add_argument("dataset", choices=["submm_obs"])
    dataset_reconcile = _add_parser(dataset_commands, "reconcile", "Re-run curated dataset association logic.", "Attempts to associate current dataset records to targets using the latest identifiers and astrometry. This is safe to rerun after adding targets or improving matching rules.")
    dataset_reconcile.add_argument("dataset", choices=["submm_obs"])
    dataset_associate = _add_parser(dataset_commands, "associate", "Manually associate one curated record with a target.", "Records an audited association for a dataset record that automatic matching could not resolve. Use record_no from dataset review output.")
    dataset_associate.add_argument("dataset", choices=["submm_obs"])
    dataset_associate.add_argument("record_no", type=int)
    dataset_associate.add_argument("target")
    dataset_associate.add_argument("--actor", required=True)
    dataset_associate.add_argument("--reason", required=True)
    dataset_unassociate = _add_parser(dataset_commands, "unassociate", "Remove a manual curated-record association.", "Records an audited unassociation without deleting the curated record. Use this when a previous association was wrong or superseded.")
    dataset_unassociate.add_argument("dataset", choices=["submm_obs"])
    dataset_unassociate.add_argument("record_no", type=int)
    dataset_unassociate.add_argument("--actor", required=True)
    dataset_unassociate.add_argument("--reason", required=True)
    for action in ("exclude", "include"):
        dataset_override = _add_parser(dataset_commands, action, f"{action.title()} one curated dataset record for export.", "This records an audited inclusion/exclusion decision for curated photometry. The underlying source-controlled record remains unchanged.")
        dataset_override.add_argument("dataset", choices=["submm_obs"])
        dataset_override.add_argument("record_no", type=int)
        dataset_override.add_argument("--actor", required=True)
        dataset_override.add_argument("--reason", required=True)
    dataset_pending = _add_parser(dataset_commands, "pending", "List targets with pending curated-data export work.", "Shows dataset changes that have not yet flowed through to target exports. Use this to decide which rawphot files need regeneration.")
    dataset_pending.add_argument("dataset", choices=["submm_obs"])
    dataset_mark_exported = _add_parser(dataset_commands, "mark-exported", "Mark curated-data export work complete for a target.", "Clears pending curated export state after an external export step. This is mainly for controlled workflows where export completion is handled outside `sdb export-dirty`.")
    dataset_mark_exported.add_argument("dataset", choices=["submm_obs"])
    dataset_mark_exported.add_argument("target")
    reference = _add_parser(commands, "reference", "Fetch, inspect, apply, and audit whole-catalog reference snapshots.", "Reference snapshots store provider tables in a separate SQLite database for fast local matching. Use these commands for catalogs where full-table ingestion is preferable to per-target remote queries.")
    reference_commands = reference.add_subparsers(dest="reference_command", required=True)
    reference_fetch = None
    for action in ("fetch", "status", "references", "relationships", "readme"):
        command = _add_parser(reference_commands, action, f"{action.title()} reference snapshot information.", "Reference commands operate on the separate reference SQLite database. Use fetch to download a snapshot, status/readme/describe to inspect it, and references/relationships to inspect catalog metadata.")
        command.add_argument("adapter", choices=REFERENCE_ADAPTERS)
        if action == "fetch":
            reference_fetch = command
    assert reference_fetch is not None
    reference_fetch.add_argument("--refresh-cache", action="store_true", help="download a fresh raw-provider snapshot before importing")
    reference_fetch.add_argument("--no-cache", action="store_true", help="fetch directly into the reference database without using the snapshot cache")
    reference_describe = _add_parser(reference_commands, "describe", "Describe tables and columns in a reference snapshot.", "Shows VizieR-derived table metadata, column descriptions, units, and stable local table names. Add a table name to focus on one table.")
    reference_describe.add_argument("adapter", choices=REFERENCE_ADAPTERS)
    reference_describe.add_argument("table", nargs="?")
    reference_apply = _add_parser(reference_commands, "apply", "Apply a reference snapshot to current targets.", "Runs local catalog matching for all targets using a fetched reference snapshot. Results are versioned like remote catalog refreshes and can produce matched, no-match, or ambiguous outcomes.")
    reference_apply.add_argument("adapter", choices=REFERENCE_ADAPTERS)
    reference_apply.add_argument("--all", action="store_true", dest="apply_all")
    reference_apply.add_argument("--force", action="store_true")
    reference_audit = _add_parser(reference_commands, "audit-identifiers", "Audit catalog identifiers against SIMBAD identifiers.", "Checks whether position-matched catalog rows with meaningful identifiers agree with target SIMBAD aliases. Use --problems-only to focus on conflicts and missing expected identifiers.")
    reference_audit.add_argument("adapter", choices=REFERENCE_ADAPTERS)
    reference_audit.add_argument("--all-targets", action="store_true")
    reference_audit.add_argument("--problems-only", action="store_true")
    for action in ("application-status", "review", "pending"):
        command = _add_parser(reference_commands, action, f"{action.title()} reference application state.", "Use application-status to inspect previous local snapshot applications, review to inspect unmatched or ambiguous rows, and pending to see targets needing export after reference changes.")
        command.add_argument("adapter", choices=REFERENCE_ADAPTERS)
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
    from .catalogs import CatalogService
    from .metadata import MetadataService

    if offline:
        identity_factory = lambda: IdentityService(sessions)
        metadata_factory = lambda: MetadataService(sessions, None)
        catalog_factory = lambda: CatalogService(sessions, {})
    else:
        from .live_providers import AstroqueryGaia, AstroquerySimbad
        from .simbad_metadata import AstroquerySimbadMetadata
        from .adapters.allwise import AllWiseAdapter
        from .adapters.gaia import GaiaDr3Adapter
        from .adapters.twomass import TwoMassAdapter
        from .adapters.tycho2 import Tycho2Adapter

        identity_factory = lambda: IdentityService(
            sessions,
            simbad=AstroquerySimbad(),
            gaia=AstroqueryGaia(),
        )
        metadata_factory = lambda: MetadataService(sessions, AstroquerySimbadMetadata())
        catalog_factory = lambda: CatalogService(
            sessions,
            {
                "gaia_dr3": GaiaDr3Adapter(),
                "tycho2": Tycho2Adapter(),
                "2mass": TwoMassAdapter(),
                "allwise": AllWiseAdapter(),
            },
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
    from .catalogs import CatalogService
    from .metadata import MetadataService
    from .reference import ReferenceStore
    from .update import UpdateService

    if offline:
        metadata_factory = lambda: MetadataService(sessions, None)
        catalog_factory = lambda: CatalogService(sessions, {})
    else:
        from .adapters.allwise import AllWiseAdapter
        from .adapters.gaia import GaiaDr3Adapter
        from .adapters.twomass import TwoMassAdapter
        from .adapters.tycho2 import Tycho2Adapter
        from .simbad_metadata import AstroquerySimbadMetadata

        metadata_factory = lambda: MetadataService(
            sessions, AstroquerySimbadMetadata()
        )
        catalog_factory = lambda: CatalogService(sessions, {
            "gaia_dr3": GaiaDr3Adapter(),
            "tycho2": Tycho2Adapter(),
            "2mass": TwoMassAdapter(),
            "allwise": AllWiseAdapter(),
        })
    return UpdateService(
        sessions,
        ReferenceStore(reference_database),
        metadata_factory=metadata_factory,
        catalog_factory=catalog_factory,
        workers=workers,
        bulk_chunk_size=bulk_chunk_size,
        reporter=reporter,
    )


def _format_json(args, value, **kwargs) -> str:
    kwargs.setdefault("sort_keys", True)
    compact = getattr(args, "compact_json", False) or _is_json_record_stream(args)
    if compact:
        kwargs.pop("indent", None)
        kwargs.setdefault("separators", (",", ":"))
    else:
        kwargs["indent"] = kwargs.get("indent", 2)
    return json.dumps(value, **kwargs)


def _photometry_review_priority_rank(priority: str) -> int:
    return {"none": 0, "low": 1, "medium": 2, "high": 3, "highest": 4}.get(priority, 0)


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
    from .review_widget import build_review_sky_view, write_review_sky_html

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
        predicted = row.get("predicted_scope") or row.get("predicted_blend_status") or ""
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


def _is_json_record_stream(args) -> bool:
    command = getattr(args, "command", None)
    if command in {
        "attributes",
        "catalog-status",
        "dirty",
        "export-dirty",
        "metadata-status",
        "review",
    }:
        return True
    if command == "alma":
        return getattr(args, "alma_command", None) in {"projects", "status"}
    if command == "cache":
        return getattr(args, "cache_command", None) in {"status", "tables"}
    if command == "dataset":
        return getattr(args, "dataset_command", None) in {"pending", "review", "status"}
    if command == "hierarchy":
        hierarchy_command = getattr(args, "hierarchy_command", None)
        if hierarchy_command in {"photometry-review", "review-queue"}:
            return getattr(args, "format", None) == "jsonl"
        return hierarchy_command in {
            "graph",
            "graph-diagnostics",
            "review",
            "sources",
        }
    if command == "reference":
        return getattr(args, "reference_command", None) in {
            "application-status",
            "audit-identifiers",
            "describe",
            "pending",
            "references",
            "relationships",
            "review",
        }
    if command == "photometry":
        return (
            getattr(args, "photometry_command", None) == "review"
            or (
                getattr(args, "photometry_command", None) == "review-queue"
                and getattr(args, "format", None) == "jsonl"
            )
        )
    if command == "sample":
        return getattr(args, "sample_command", None) in {"list", "members"}
    return False


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    from .progress import ProgressReporter

    reporter = ProgressReporter.for_cli(quiet=args.quiet, force=args.progress)
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
        from .cache_store import SnapshotCache

        cache = SnapshotCache(args.cache_database)
        try:
            if args.cache_command == "status":
                for value in cache.summaries(include_old=args.cache_all):
                    print(_format_json(args, asdict(value), sort_keys=True))
            elif args.cache_command == "tables":
                snapshot = cache.current_snapshot_for_catalog(
                    args.catalog, provider=args.provider
                )
                if snapshot is None:
                    raise KeyError(f"cached snapshot not found: {args.catalog}")
                for table in snapshot.tables:
                    columns = table.metadata.get("columns", [])
                    print(_format_json(args, {
                        "provider": snapshot.provider,
                        "catalog": snapshot.catalog_id,
                        "source_id": snapshot.source_id,
                        "table": table.name,
                        "description": table.description,
                        "row_count": len(table.rows),
                        "columns": columns,
                    }, sort_keys=True))
            elif args.cache_command == "readme":
                snapshot = cache.current_snapshot_for_catalog(
                    args.catalog, provider=args.provider
                )
                if snapshot is None:
                    raise KeyError(f"cached snapshot not found: {args.catalog}")
                print(snapshot.readme)
            elif args.cache_command == "validate":
                value = asdict(cache.validate(args.catalog, provider=args.provider))
                reference_path = Path(args.reference_database)
                if reference_path.exists():
                    from .reference import ReferenceStore
                    from .reference_definitions import SNAPSHOT_CATALOGS

                    reference = ReferenceStore(reference_path)
                    comparisons = []
                    for adapter, definition in SNAPSHOT_CATALOGS.items():
                        if definition.catalog != value["catalog_id"]:
                            continue
                        snapshot = reference.current_snapshot(adapter)
                        if snapshot is None:
                            continue
                        row_count = sum(
                            item["row_count"]
                            for item in reference.describe(adapter=adapter)
                        )
                        comparisons.append({
                            "adapter": adapter,
                            "snapshot_id": snapshot.id,
                            "content_sha256": snapshot.content_sha256,
                            "row_count": row_count,
                            "row_count_matches_cache": row_count == value["row_count"],
                        })
                    if comparisons:
                        value["reference_snapshots"] = comparisons
                print(_format_json(args, value, sort_keys=True))
                return 0 if value["ok"] else 1
            return 0
        except (KeyError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
    path = Path(args.database)
    if args.command == "init":
        init_database(path)
        print(path.resolve())
        return 0
    if args.command == "reference":
        from .reference import ReferenceApplicationService, ReferenceStore

        store = ReferenceStore(args.reference_database)
        try:
            if args.reference_command == "fetch":
                value = store.fetch(
                    args.adapter,
                    cache_path=None if args.no_cache else args.cache_database,
                    refresh_cache=args.refresh_cache,
                )
                print(_format_json(args, value.__dict__, sort_keys=True))
            elif args.reference_command == "status":
                value = store.current_snapshot(args.adapter)
                if value is None:
                    raise KeyError(f"reference snapshot not found: {args.adapter}")
                print(_format_json(args, {
                    "snapshot_id": value.id,
                    "adapter": value.adapter,
                    "catalog": value.catalog,
                    "content_sha256": value.content_sha256,
                    "source_url": value.source_url,
                    "retrieved_at": value.retrieved_at.isoformat(),
                }, sort_keys=True))
            elif args.reference_command == "describe":
                values = store.describe(adapter=args.adapter)
                if args.table:
                    values = [
                        value for value in values
                        if value["name"] == args.table
                        or value["name"].rsplit("/", 1)[-1] == args.table
                    ]
                    if not values:
                        raise KeyError(f"reference table not found: {args.table}")
                for value in values:
                    print(_format_json(args, value, sort_keys=True))
            elif args.reference_command == "relationships":
                for value in store.relationships(adapter=args.adapter):
                    print(_format_json(args, {
                        "from_table": value.from_table,
                        "from_column": value.from_column,
                        "to_table": value.to_table,
                        "to_column": value.to_column,
                        "parser": value.parser,
                        "description": value.description,
                    }, sort_keys=True))
            elif args.reference_command == "references":
                values = store.rows("refs", adapter=args.adapter)
                for value in values:
                    print(_format_json(args, value, sort_keys=True))
            elif args.reference_command == "readme":
                value = store.current_snapshot(args.adapter)
                if value is None:
                    raise KeyError(f"reference snapshot not found: {args.adapter}")
                print(value.readme)
            else:
                if not path.exists():
                    raise KeyError(f"database does not exist: {path}; run 'sdb init'")
                application = ReferenceApplicationService(
                    make_session_factory(path), store
                )
                if args.reference_command == "apply":
                    if not args.apply_all:
                        raise ValueError("reference apply requires --all")
                    value = application.apply(args.adapter, force=args.force)
                    print(_format_json(args, value.__dict__, sort_keys=True))
                elif args.reference_command == "application-status":
                    for value in application.runs(args.adapter):
                        print(_format_json(args, {
                            "application_run_id": value.id,
                            "provider": value.provider,
                            "snapshot_sha256": value.snapshot_sha256,
                            "status": value.status,
                            "targets": value.target_count,
                            "refreshed": value.refreshed_count,
                            "matched": value.match_count,
                            "ambiguous": value.ambiguous_count,
                            "no_match": value.no_match_count,
                            "catalog_rows": value.row_count,
                            "unmatched_rows": value.unmatched_row_count,
                        }, sort_keys=True))
                elif args.reference_command == "audit-identifiers":
                    from .identifier_audit import audit_catalog_identifiers

                    values = audit_catalog_identifiers(
                        make_session_factory(path),
                        args.adapter,
                        include_unmatched=args.all_targets,
                    )
                    for value in values:
                        if args.problems_only and value.status == "agree":
                            continue
                        print(_format_json(args, value.__dict__, sort_keys=True))
                elif args.reference_command == "review":
                    for value in application.unmatched(provider=args.adapter):
                        print(_format_json(args, {
                            "source_identifier": value.source_identifier,
                            "status": value.status,
                            "candidate_target_ids": json.loads(value.candidate_target_ids_json),
                            "selected_target_ids": json.loads(value.selected_target_ids_json),
                        }, sort_keys=True))
                else:
                    for dirty, target, run in application.pending(args.adapter):
                        print(_format_json(args, {
                            "application_run_id": run.id,
                            "provider": run.provider,
                            "target_id": target.id,
                            "sdbid": target.sdbid,
                            "reason": dirty.reason,
                        }, sort_keys=True))
        except (KeyError, ValueError, RuntimeError, ProviderError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if not path.exists():
        print(f"database does not exist: {path}; run 'sdb init'", file=sys.stderr)
        return 2
    sessions = make_session_factory(path)
    if args.command == "alma":
        from .alma import AlmaArchiveService

        try:
            if args.alma_command == "sync":
                if args.offline:
                    raise ValueError("ALMA sync is unavailable in offline mode")
                from .alma import AstroqueryAlmaArchive

                service = AlmaArchiveService(
                    sessions, AstroqueryAlmaArchive(
                        args.archive_url, timeout_seconds=args.timeout,
                    ),
                )
                if args.bootstrap:
                    summary = service.bootstrap(
                        args.start_year, args.end_year, args.chunk_months,
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
                print(_format_json(args, asdict(summary), sort_keys=True))
            elif args.alma_command == "projects":
                # Project lookup is local and deliberately requires no archive client.
                service = AlmaArchiveService(sessions, None)
                for project in service.projects(args.target, args.radius):
                    print(_format_json(args, asdict(project), sort_keys=True))
            elif args.alma_command == "compact":
                service = AlmaArchiveService(sessions, None)
                # Compaction is local; the service only needs an endpoint label
                # for durable run provenance.
                service.provider = type(
                    "LocalAlmaCompactor", (), {"archive_url": "local-cache"}
                )()
                print(_format_json(args, 
                    asdict(service.compact_observations()), sort_keys=True
                ))
            elif args.alma_command == "rebuild-bounds":
                service = AlmaArchiveService(sessions, None)
                print(_format_json(args, {
                    "updated": service.rebuild_member_bounds()
                }, sort_keys=True))
            elif args.alma_command == "rebuild-positions":
                service = AlmaArchiveService(sessions, None)
                print(_format_json(args, {
                    "inserted": service.rebuild_member_positions()
                }, sort_keys=True))
            else:
                from .models import AlmaSyncRun

                if args.limit < 1:
                    raise ValueError("--limit must be at least 1")
                with sessions() as session:
                    runs = list(session.scalars(
                        select(AlmaSyncRun)
                        .order_by(AlmaSyncRun.id.desc())
                        .limit(args.limit)
                    ))
                for run in runs:
                    print(_format_json(args, {
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
                    }, sort_keys=True))
        except (KeyError, ValueError, RuntimeError, ProviderError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
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
        from .samples import SampleService

        service = SampleService(sessions)
        try:
            if args.sample_command == "create":
                value = service.create(args.name, sample_date=args.date, note=args.note)
                print(_format_json(args, {"id": value.id, "name": value.name}, sort_keys=True))
            elif args.sample_command == "set":
                value = service.set_metadata(
                    args.name, sample_date=args.date, note=args.note,
                )
                print(_format_json(args, {"id": value.id, "name": value.name}, sort_keys=True))
            elif args.sample_command == "list":
                for value in service.list():
                    data = asdict(value)
                    data["sample_date"] = (
                        value.sample_date.isoformat() if value.sample_date else None
                    )
                    print(_format_json(args, data, sort_keys=True))
            elif args.sample_command in {"add", "remove"}:
                value = getattr(service, args.sample_command)(
                    args.name, args.target, actor=args.actor, reason=args.reason,
                )
                print(_format_json(args, {
                    "action_id": value.id,
                    "action": value.action,
                    "sample_id": value.sample_id,
                    "target_id": value.target_id,
                }, sort_keys=True))
            elif args.sample_command == "members":
                for target in service.members(args.name):
                    print(_format_json(args, {
                        "target_id": target.id, "sdbid": target.sdbid,
                    }, sort_keys=True))
            elif args.sample_command == "readiness":
                from .readiness import ReadinessService

                providers = tuple(
                    value.strip() for value in args.providers.split(",")
                    if value.strip()
                )
                report = ReadinessService(sessions).report(
                    args.name, providers=providers,
                )
                print(_format_json(args, asdict(report), sort_keys=True))
                return 1 if report.status == "blocked" else 0
            else:
                print(_format_json(args, service.import_members(
                    args.name, args.file, actor=args.actor, reason=args.reason,
                ), sort_keys=True))
        except (KeyError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
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
        from .dirty import find_target, pending_export_targets
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
            service = _update_service(
                sessions,
                args.reference_database,
                workers=args.workers,
                bulk_chunk_size=args.chunk_size,
                offline=args.offline,
                reporter=reporter,
            )
            if args.update_all:
                summary = service.update_all(force=args.force, providers=providers)
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
                        target = find_target(session, args.target)
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
        if args.offline:
            service = IdentityService(sessions)
        else:
            # Astroquery's Gaia module performs service-status setup at import
            # time; keep it out of offline and database-inspection commands.
            from .live_providers import AstroqueryGaia, AstroquerySimbad

            service = IdentityService(sessions, simbad=AstroquerySimbad(), gaia=AstroqueryGaia())
        try:
            added = service.add(AddRequest(name=args.name, ra_deg=args.ra, dec_deg=args.dec, epoch=args.epoch, command=" ".join(sys.argv)))
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
        from .review_widget import build_review_sky_view, write_review_sky_html

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
        from .catalogs import CatalogService

        with sessions() as session:
            raw = session.get(RawCatalogRow, args.candidate_id)
            run = None if raw is None else session.get(CatalogRun, raw.run_id)
        if run is None:
            print("catalog candidate not found", file=sys.stderr)
            return 2
        if run.provider in REFERENCE_ADAPTERS:
            from .reference import ReferenceStore, snapshot_adapter
            adapter = snapshot_adapter(
                run.provider, ReferenceStore(args.reference_database)
            )
        elif run.provider == "2mass":
            from .adapters.twomass import TwoMassAdapter
            adapter = TwoMassAdapter()
        elif run.provider == "allwise":
            from .adapters.allwise import AllWiseAdapter
            adapter = AllWiseAdapter()
        elif run.provider == "gaia_dr3":
            from .adapters.gaia import GaiaDr3Adapter
            adapter = GaiaDr3Adapter()
        elif run.provider == "tycho2":
            from .adapters.tycho2 import Tycho2Adapter
            adapter = Tycho2Adapter()
        else:
            print(f"catalog adapter is unavailable: {run.provider}", file=sys.stderr)
            return 2
        try:
            value = CatalogService(
                sessions, {run.provider: adapter}
            ).override_candidate(
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
            if args.provider in {"2mass", "allwise", "gaia_dr3", "tycho2", *REFERENCE_ADAPTERS}:
                from .catalogs import CatalogService
                if args.provider in REFERENCE_ADAPTERS:
                    from .reference import ReferenceStore, snapshot_adapter
                    adapters = {
                        args.provider: snapshot_adapter(
                            args.provider, ReferenceStore(args.reference_database)
                        )
                    }
                else:
                    from .adapters.allwise import AllWiseAdapter
                    from .adapters.gaia import GaiaDr3Adapter
                    from .adapters.twomass import TwoMassAdapter
                    from .adapters.tycho2 import Tycho2Adapter
                    adapters = {
                        "gaia_dr3": GaiaDr3Adapter(),
                        "tycho2": Tycho2Adapter(),
                        "2mass": TwoMassAdapter(),
                        "allwise": AllWiseAdapter(),
                    }
                refreshed = CatalogService(
                    sessions, adapters,
                ).refresh(
                    args.target, args.provider
                )
            else:
                from .metadata import MetadataService
                from .simbad_metadata import AstroquerySimbadMetadata

                refreshed = MetadataService(sessions, AstroquerySimbadMetadata()).refresh(args.target)
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
            list_photometry_overrides,
            review_photometry_associations,
            set_photometry_override,
            unassign_measurement_target,
        )

        try:
            if args.photometry_command in {"exclude", "include"}:
                value = set_photometry_override(
                    sessions,
                    args.target,
                    provider=args.provider,
                    band=args.band,
                    excluded=args.photometry_command == "exclude",
                    actor=args.actor,
                    reason=args.reason,
                )
                values = [value]
                for value in values:
                    print(_format_json(args, {
                        "id": value.id,
                        "target_id": value.target_id,
                        "provider": value.provider,
                        "band": value.band,
                        "excluded": value.excluded,
                        "actor": value.actor,
                        "reason": value.reason,
                        "created_at": value.created_at.isoformat(),
                    }, sort_keys=True))
            elif args.photometry_command == "overrides":
                for value in list_photometry_overrides(sessions, args.target):
                    print(_format_json(args, {
                        "id": value.id,
                        "target_id": value.target_id,
                        "provider": value.provider,
                        "band": value.band,
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
                from .review_widget import build_review_sky_view, write_review_sky_html

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

                print(_format_json(
                    args,
                    measurement_assignment_proposals(sessions, args.target),
                    sort_keys=True,
                ))
            elif args.photometry_command == "apply-proposals":
                from .proposal_application import apply_measurement_assignment_proposals

                print(_format_json(
                    args,
                    apply_measurement_assignment_proposals(
                        sessions,
                        target_reference=args.target,
                        sample=args.sample,
                        apply=args.apply,
                        actor=args.actor,
                        reason=args.reason,
                        reporter=reporter,
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
            target = session.scalar(select(Target).where(Target.sdbid == args.target))
            if target is None and args.target.isdigit():
                target = session.get(Target, int(args.target))
            if target is None:
                from .service import normalize_identifier
                from .models import ExternalIdentifier
                identifier = session.scalar(select(ExternalIdentifier).where(
                    ExternalIdentifier.normalized_value == normalize_identifier(args.target)
                ).limit(1))
                target = None if identifier is None else session.get(Target, identifier.target_id)
            if target is None:
                print("target not found", file=sys.stderr)
                return 2
            query = (
                select(CatalogAttribute)
                .join(CatalogRun, CatalogRun.id == CatalogAttribute.run_id)
                .where(
                    CatalogAttribute.target_id == target.id,
                    CatalogRun.is_current.is_(True),
                    CatalogRun.status == "match",
                )
                .order_by(CatalogAttribute.key, CatalogAttribute.provider)
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
                    MetadataRun.status == "match",
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
        from .datasets import CuratedDatasetService

        service = CuratedDatasetService(sessions)
        try:
            if args.dataset_command == "import":
                value = service.import_submm_obs(args.file)
                print(_format_json(args, value.__dict__, sort_keys=True))
            elif args.dataset_command == "status":
                for value in service.revisions(args.dataset):
                    print(_format_json(args, {
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
                    }, sort_keys=True))
            elif args.dataset_command == "review":
                for value in service.unresolved(args.dataset):
                    print(_format_json(args, {
                        "record_no": value.record_no,
                        "identifier": value.source_identifier,
                        "status": value.association_status,
                        "message": value.association_message,
                    }, sort_keys=True))
            elif args.dataset_command == "reconcile":
                value = service.reconcile(args.dataset)
                print(_format_json(args, value.__dict__, sort_keys=True))
            elif args.dataset_command == "associate":
                value = service.associate(
                    args.dataset, args.record_no, args.target,
                    actor=args.actor, reason=args.reason,
                )
                print(_format_json(args, {
                    "action_id": value.id, "dataset": value.dataset,
                    "record_no": value.record_no, "action": value.action,
                    "target_id": value.target_id,
                }, sort_keys=True))
            elif args.dataset_command == "unassociate":
                value = service.unassociate(
                    args.dataset, args.record_no,
                    actor=args.actor, reason=args.reason,
                )
                print(_format_json(args, {
                    "action_id": value.id, "dataset": value.dataset,
                    "record_no": value.record_no, "action": value.action,
                    "target_id": value.target_id,
                }, sort_keys=True))
            elif args.dataset_command in {"exclude", "include"}:
                value = service.set_record_override(
                    args.dataset, args.record_no,
                    excluded=args.dataset_command == "exclude",
                    actor=args.actor, reason=args.reason,
                )
                print(_format_json(args, {
                    "override_id": value.id, "dataset": value.dataset,
                    "record_no": value.record_no, "excluded": value.excluded,
                }, sort_keys=True))
            elif args.dataset_command == "pending":
                for dirty, target in service.pending(args.dataset):
                    print(_format_json(args, {
                        "dirty_id": dirty.id,
                        "revision_id": None if dirty.source_id is None else int(dirty.source_id),
                        "target_id": target.id, "sdbid": target.sdbid,
                        "reason": dirty.reason,
                    }, sort_keys=True))
            else:
                count = service.mark_exported(args.dataset, args.target)
                print(_format_json(args, {"marked_exported": count}, sort_keys=True))
        except (OSError, ValueError, KeyError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if args.command == "import":
        refresh = tuple(value.strip() for value in args.refresh.split(",") if value.strip())
        if args.offline and refresh:
            print("remote refresh is unavailable in offline mode", file=sys.stderr)
            return 2
        try:
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
        from .review_ui import serve_review_ui

        try:
            identity_service_factory = None
            if not args.offline:
                from .live_providers import AstroqueryGaia, AstroquerySimbad

                identity_service_factory = lambda: IdentityService(
                    sessions,
                    simbad=AstroquerySimbad(),
                    gaia=AstroqueryGaia(),
                )
            serve_review_ui(
                sessions,
                sample=args.sample,
                host=args.host,
                port=args.port,
                open_browser=args.open,
                identity_service_factory=identity_service_factory,
            )
        except (RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    with sessions() as session:
        if args.command == "status":
            from .dirty import resolve_targets
            targets = resolve_targets(session, args.target)
            if not targets:
                print(f"target not found: {args.target}", file=sys.stderr)
                return 1
            from .target_lifecycle import target_lifecycle_status
            from .hierarchy import HierarchyService
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
                status_payload["hierarchy"] = HierarchyService(sessions).target_context_summary(target.sdbid)
                print(_format_json(args, status_payload, sort_keys=True))
            return 0
        if args.command == "runs":
            from .models import MetadataRun
            from .dirty import resolve_targets

            targets = resolve_targets(session, args.target)
            if not targets:
                print(f"target not found: {args.target}", file=sys.stderr)
                return 1
            want_catalog = args.provider != "simbad"
            want_metadata = args.provider in (None, "simbad")
            for target in targets:
                if want_catalog:
                    query = select(CatalogRun).where(CatalogRun.target_id == target.id)
                    if args.provider:
                        query = query.where(CatalogRun.provider == args.provider)
                    for run in session.scalars(query.order_by(CatalogRun.id.desc())):
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
                psc = session.get(CatalogRun, family.psc_run_id)
                fsc = session.get(CatalogRun, family.fsc_run_id)
                print(_format_json(args, {
                    "family_id": family.id,
                    "target_id": family.target_id,
                    "sdbid": target.sdbid,
                    "status": family.status,
                    "normalized_separation": family.normalized_separation,
                    "reason": family.reason,
                    "psc_run_id": family.psc_run_id,
                    "psc_source_id": psc.selected_source_id,
                    "fsc_run_id": family.fsc_run_id,
                    "fsc_source_id": fsc.selected_source_id,
                    "band_selections": [{
                        "band": value.band,
                        "selected_measurement_id": value.selected_measurement_id,
                        "alternate_measurement_id": value.alternate_measurement_id,
                        "reason": value.reason,
                    } for value in selections],
                }, sort_keys=True))
        elif args.kind == "catalog-matches":
            candidates = session.execute(
                select(RawCatalogRow, CatalogRun)
                .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
                .where(
                    CatalogRun.is_current.is_(True),
                    CatalogRun.status == "ambiguous",
                )
                .order_by(CatalogRun.id, RawCatalogRow.score.desc())
            )
            for candidate, run in candidates:
                print(_format_json(args, {
                    "candidate_id": candidate.id,
                    "run_id": run.id,
                    "target_id": run.target_id,
                    "provider": run.provider,
                    "source_id": candidate.source_id,
                    "separation_arcsec": candidate.separation_arcsec,
                    "score": candidate.score,
                }, sort_keys=True))
        else:
            from sqlalchemy import case, func
            from .models import Submission

            ambiguous = session.scalars(
                select(Submission)
                .join(MatchCandidate, MatchCandidate.submission_id == Submission.id)
                .group_by(Submission.id)
                .having(func.sum(case((MatchCandidate.accepted, 1), else_=0)) == 0)
                .order_by(Submission.id)
            )
            for submission in ambiguous:
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
