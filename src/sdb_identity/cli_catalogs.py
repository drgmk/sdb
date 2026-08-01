"""Catalog refresh, status, attributes, and decision CLI commands."""

from __future__ import annotations

import sys

from sqlalchemy import select

from .catalog_registry import SNAPSHOT_CATALOG_PROVIDERS
from .catalog_results import effective_catalog_results
from .cli_context import CliContext
from .models.catalogs import CatalogRun, RawCatalogRow
from .targets import resolve_target, resolve_targets
from .vocabulary import ProviderRunStatus


CATALOG_COMMANDS = {"override-catalog-match", "refresh", "runs", "attributes"}


def register_catalog_parsers(
    commands,
    add_parser,
    add_actor_argument,
    add_reason_argument,
) -> None:
    override = add_parser(
        commands,
        "override-catalog-match",
        "Manually accept a catalog photometry match candidate.",
        "This changes the current catalog association through an audited override "
        "rather than editing raw provider rows. Use it for ambiguous catalog "
        "matches after checking source IDs, separation, and notes.",
    )
    override.add_argument("candidate_id", type=int)
    add_actor_argument(override)
    add_reason_argument(override)
    refresh = add_parser(
        commands,
        "refresh",
        "Refresh one provider for one target.",
        "Runs a single catalog or metadata provider and stores a versioned result. "
        "Previous rows remain available for provenance; current rows are updated "
        "only after a successful provider attempt.",
    )
    refresh.add_argument("target")
    refresh.add_argument(
        "--provider",
        choices=[
            "2mass", "allwise", "gaia_dr3", "tycho2",
            *SNAPSHOT_CATALOG_PROVIDERS, "simbad",
        ],
        required=True,
    )
    runs = add_parser(
        commands,
        "runs",
        "Show catalog and metadata provider run status for one target.",
        "Reports which providers matched, failed, returned no match, or are "
        "ambiguous, across both catalog photometry and SIMBAD metadata. Each row "
        "is tagged with its kind. Add --provider to focus on one provider.",
    )
    runs.add_argument("target")
    runs.add_argument(
        "--provider",
        choices=[
            "2mass", "allwise", "gaia_dr3", "tycho2",
            *SNAPSHOT_CATALOG_PROVIDERS, "simbad",
        ],
    )
    attributes = add_parser(
        commands,
        "attributes",
        "Show current catalog attributes for one target.",
        "Attributes are non-photometric catalog values such as ages or flags "
        "copied from provider rows. They are versioned like photometry and can be "
        "filtered by --key.",
    )
    attributes.add_argument("target")
    attributes.add_argument("--key")


def run_catalog_command(context: CliContext) -> int:
    args = context.args
    if args.command == "override-catalog-match":
        return _run_override(context)
    if args.command == "refresh":
        return _run_refresh(context)
    if args.command == "runs":
        return _run_runs(context)
    if args.command == "attributes":
        return _run_attributes(context)
    raise ValueError(f"unknown catalog command: {args.command}")


def _run_override(context: CliContext) -> int:
    from .catalog_decisions import CatalogDecisionService
    from .catalog_registry import build_catalog_adapter
    from .reference import ReferenceStore

    args = context.args
    sessions = context.require_sessions()
    with sessions() as session:
        raw = session.get(RawCatalogRow, args.candidate_id)
        run = None if raw is None else session.get(CatalogRun, raw.run_id)
    if run is None:
        print("catalog candidate not found", file=sys.stderr)
        return 2
    try:
        adapter = build_catalog_adapter(
            run.provider,
            reference_store=ReferenceStore(context.reference_database_path),
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        value = CatalogDecisionService(
            sessions, {run.provider: adapter},
        ).accept_candidate(
            args.candidate_id,
            actor=args.actor,
            reason=args.reason,
        )
    except (KeyError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json(value.__dict__, sort_keys=True))
    return 0


def _run_refresh(context: CliContext) -> int:
    args = context.args
    sessions = context.require_sessions()
    if args.offline and args.provider not in SNAPSHOT_CATALOG_PROVIDERS:
        print("remote refresh is unavailable in offline mode", file=sys.stderr)
        return 2
    try:
        with context.provider_output():
            from .catalog_registry import CATALOG_PROVIDERS

            if args.provider in CATALOG_PROVIDERS:
                from .catalog_acquisition import CatalogAcquisitionService
                from .catalog_registry import build_catalog_adapter
                from .reference import ReferenceStore

                adapters = {args.provider: build_catalog_adapter(
                    args.provider,
                    reference_store=ReferenceStore(
                        context.reference_database_path,
                    ),
                )}
                refreshed = CatalogAcquisitionService(
                    sessions, adapters,
                ).refresh(args.target, args.provider)
            else:
                from .metadata import MetadataService
                from .simbad_metadata import AstroquerySimbadMetadata

                refreshed = MetadataService(
                    sessions, AstroquerySimbadMetadata(),
                ).refresh(args.target)
    except (KeyError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(context.json(refreshed.__dict__, sort_keys=True))
    return 0


def _run_runs(context: CliContext) -> int:
    from .models.metadata import MetadataRun

    args = context.args
    sessions = context.require_sessions()
    with sessions() as session:
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
                    current_result = effective.get((target.id, run.provider))
                    print(context.json({
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
                    print(context.json({
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


def _run_attributes(context: CliContext) -> int:
    from .models.catalogs import CatalogAttribute
    from .models.metadata import MetadataRun, SimbadMetadata

    args = context.args
    sessions = context.require_sessions()
    with sessions() as session:
        target = resolve_target(session, args.target)
        if target is None:
            print("target not found", file=sys.stderr)
            return 2
        matched_run_ids = {
            value.run.id
            for value in effective_catalog_results(session, [target.id]).values()
            if value.status == ProviderRunStatus.MATCH
        }
        query = select(CatalogAttribute).where(
            CatalogAttribute.target_id == target.id,
            CatalogAttribute.run_id.in_(matched_run_ids),
        ).order_by(CatalogAttribute.key, CatalogAttribute.provider)
        if args.key:
            query = query.where(CatalogAttribute.key == args.key)
        for value in session.scalars(query):
            print(context.json({
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
            _write_simbad_attributes(context, simbad)
    return 0


def _write_simbad_attributes(context: CliContext, simbad) -> None:
    args = context.args
    values = (
        ("spectral_type", simbad.spectral_type, None, None, simbad.spectral_type_bibcode),
        ("parallax", None, simbad.parallax_mas, "mas", simbad.parallax_bibcode),
        (
            "radial_velocity", None, simbad.radial_velocity_kms, "km/s",
            simbad.radial_velocity_bibcode,
        ),
    )
    for key, text_value, float_value, unit, reference in values:
        if args.key and args.key != key:
            continue
        if text_value is None and float_value is None:
            continue
        print(context.json({
            "provider": "simbad",
            "source_id": simbad.main_id,
            "key": key,
            "value_text": text_value,
            "value_float": float_value,
            "uncertainty": (
                simbad.parallax_error_mas if key == "parallax"
                else simbad.radial_velocity_error_kms
                if key == "radial_velocity"
                else None
            ),
            "unit": unit,
            "quality": None,
            "reference": reference,
            "note": None,
        }, sort_keys=True))
