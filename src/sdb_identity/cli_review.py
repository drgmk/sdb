"""Operator review queues, sky pages, and local review server CLI commands."""

from __future__ import annotations

import sys

from sqlalchemy import select

from .catalogs.results import effective_catalog_results
from .cli_context import CliContext
from .models.catalogs import CatalogRun, RawCatalogRow
from .models.identity import MatchCandidate, Target
from .service import IdentityService
from .vocabulary import ProviderRunStatus


REVIEW_COMMANDS = {"review", "review-view"}


def register_review_parsers(commands, add_parser) -> None:
    review = add_parser(
        commands,
        "review",
        "Inspect review queues or launch the local assignment UI.",
        "Use this to inspect ambiguous identity/catalog candidates and unresolved "
        "IRAS families, or use `review serve --sample NAME` for the localhost-only "
        "system-photometry workspace. Queue output is JSON; browser changes require "
        "preview followed by an audited apply.",
    )
    review.add_argument(
        "kind",
        choices=["matches", "catalog-matches", "iras-families", "serve"],
    )
    review.add_argument("--all", action="store_true", dest="review_all")
    review.add_argument(
        "--sample",
        help="sample readiness queue shown by review serve",
    )
    review.add_argument(
        "--host",
        default="127.0.0.1",
        help="review serve bind host (localhost only)",
    )
    review.add_argument(
        "--port", type=int, default=8765, help="review serve TCP port",
    )
    review.add_argument(
        "--open",
        action="store_true",
        help="open the local review UI in a browser",
    )
    view = add_parser(
        commands,
        "review-view",
        "Write an interactive sky-view HTML page for one target.",
        "The Plotly view plots the target, nearby SDB targets, proper-motion "
        "arrows, identity candidates, catalog candidate rows, hierarchy "
        "candidates, and WDS/CCDM separation/PA geometry. It is read-only for now, "
        "but includes candidate/run identifiers needed for future override actions.",
    )
    view.add_argument("target")
    view.add_argument("--output", required=True)
    view.add_argument("--radius", type=float)
    view.add_argument(
        "--open",
        action="store_true",
        help="open the generated HTML in the default browser",
    )


def run_review_command(context: CliContext) -> int:
    if context.args.command == "review-view":
        return _run_review_view(context)
    if context.args.kind == "serve":
        return _run_review_server(context)
    return _run_review_queue(context)


def _run_review_view(context: CliContext) -> int:
    from .review_sky_render import write_review_sky_html
    from .review_widget import build_review_sky_view

    args = context.args
    try:
        view = build_review_sky_view(
            context.require_sessions(),
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
    print(context.json({
        "target_id": view.target_id,
        "sdbid": view.sdbid,
        "output": str(output.resolve()),
        "points": len(view.points),
    }, sort_keys=True))
    return 0


def _run_review_server(context: CliContext) -> int:
    from .catalogs.setup import catalog_operator_service_for_provider
    from .cli_services import build_update_service
    from .reference import ReferenceStore
    from .review_ui import serve_review_ui
    from .update import DEFAULT_PROVIDERS, REMOTE_CATALOGS

    args = context.args
    sessions = context.require_sessions()
    try:
        identity_service_factory = None
        if not args.offline:
            from .live_providers import AstroqueryGaia, AstroquerySimbad

            identity_service_factory = lambda: IdentityService(
                sessions,
                simbad=AstroquerySimbad(),
                gaia=AstroqueryGaia(),
            )
        catalog_coverage_providers = context.config.catalog_providers(
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
            catalog_update_factory = lambda: build_update_service(
                sessions,
                context.reference_database_path,
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
                    reference_database=context.reference_database_path,
                    offline=args.offline,
                    action=action,
                )
            ),
            catalog_coverage_providers=catalog_coverage_providers,
            catalog_update_factory=catalog_update_factory,
            reference_store=ReferenceStore(context.reference_database_path),
        )
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


def _run_review_queue(context: CliContext) -> int:
    args = context.args
    sessions = context.require_sessions()
    with sessions() as session:
        if args.kind == "iras-families":
            _write_iras_families(context, session)
        elif args.kind == "catalog-matches":
            _write_catalog_matches(context, session)
        else:
            _write_identity_matches(context, session)
    return 0


def _write_iras_families(context: CliContext, session) -> None:
    from .models.catalogs import IrasBandSelection, IrasDetectionFamily

    args = context.args
    query = (
        select(IrasDetectionFamily, Target)
        .join(Target, Target.id == IrasDetectionFamily.target_id)
        .where(IrasDetectionFamily.is_current.is_(True))
    )
    if not args.review_all:
        query = query.where(IrasDetectionFamily.status == "review")
    for family, target in session.execute(query.order_by(IrasDetectionFamily.id)):
        selections = list(session.scalars(
            select(IrasBandSelection)
            .where(IrasBandSelection.family_id == family.id)
            .order_by(IrasBandSelection.band)
        ))
        effective = effective_catalog_results(
            session,
            [family.target_id],
            providers=("iras_psc", "iras_fsc"),
        )
        psc = effective.get((family.target_id, "iras_psc"))
        fsc = effective.get((family.target_id, "iras_fsc"))
        print(context.json({
            "family_id": family.id,
            "target_id": family.target_id,
            "sdbid": target.sdbid,
            "status": family.status,
            "normalized_separation": family.normalized_separation,
            "reason": family.reason,
            "psc_run_id": family.psc_run_id,
            "psc_source_id": None if psc is None else psc.selected_source_id,
            "fsc_run_id": family.fsc_run_id,
            "fsc_source_id": None if fsc is None else fsc.selected_source_id,
            "band_selections": [{
                "band": value.band,
                "selected_measurement_id": value.selected_measurement_id,
                "alternate_measurement_id": value.alternate_measurement_id,
                "reason": value.reason,
            } for value in selections],
        }, sort_keys=True))


def _write_catalog_matches(context: CliContext, session) -> None:
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
        .where(CatalogRun.id.in_(ambiguous_run_ids))
        .order_by(CatalogRun.id, RawCatalogRow.score.desc())
    )
    for candidate, run, target in candidates:
        print(context.json({
            "candidate_id": candidate.id,
            "run_id": run.id,
            "target_id": run.target_id,
            "sdbid": target.sdbid,
            "provider": run.provider,
            "source_id": candidate.source_id,
            "separation_arcsec": candidate.separation_arcsec,
            "score": candidate.score,
        }, sort_keys=True))


def _write_identity_matches(context: CliContext, session) -> None:
    from .identity_results import effective_identity_candidate_ids
    from .models.identity import Submission

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
        print(context.json({
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
