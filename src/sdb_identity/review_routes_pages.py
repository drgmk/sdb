"""Read-only page and projection routes for the review server."""

from __future__ import annotations

from .catalog_overview import catalog_overview
from .review_dashboard import review_dashboard_report
from .review_pages import (
    render_catalogs_page,
    render_page,
    render_queue_page,
    render_target_page,
)
from .review_sky_render import render_review_sky_html
from .review_web_context import ReviewWebContext
from .review_widget import build_review_sky_view
from .review_workspace import build_target_workspace, queue_filters


def register_page_routes(app: object, context: ReviewWebContext) -> None:
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse

    @app.get("/", response_class=HTMLResponse)
    def index(
        view: str = "actionable",
        priority: str = "",
        role: str = "",
        classification: str = "",
        provider: str = "",
        search: str = "",
    ):
        if context.sample is None:
            return render_page(
                "SDB review",
                "<main><h1>SDB review</h1><p>Start the server with "
                "<code>--sample NAME</code> to populate the readiness queue."
                "</p></main>",
            )
        report = review_dashboard_report(
            context.session_factory,
            sample=context.sample,
        )
        return render_queue_page(
            context.sample,
            report,
            queue_filters(
                view=view,
                priority=priority,
                role=role,
                classification=classification,
                provider=provider,
                search=search,
            ),
        )

    @app.get("/target/{sdbid}", response_class=HTMLResponse)
    def target(
        sdbid: str,
        view: str = "actionable",
        priority: str = "",
        role: str = "",
        classification: str = "",
        provider: str = "",
        search: str = "",
        position: int | None = None,
    ):
        try:
            workspace = _workspace(
                context,
                sdbid,
                view=view,
                priority=priority,
                role=role,
                classification=classification,
                provider=provider,
                search=search,
                position=position,
            )
            return render_target_page(workspace)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/target/{sdbid}/sky", response_class=HTMLResponse)
    def target_sky(sdbid: str, radius: float | None = None):
        try:
            view = build_review_sky_view(
                context.session_factory,
                sdbid,
                radius_arcsec=radius,
            )
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return render_review_sky_html(view, embedded=True)

    @app.get("/api/readiness")
    def readiness_api():
        if context.sample is None:
            raise HTTPException(
                status_code=400,
                detail="server has no selected sample",
            )
        return review_dashboard_report(
            context.session_factory,
            sample=context.sample,
        )

    @app.get("/catalogs", response_class=HTMLResponse)
    def catalogs_page():
        return render_catalogs_page(catalog_overview(context.reference_store))

    @app.get("/api/catalogs")
    def catalogs_api():
        return catalog_overview(context.reference_store)

    @app.get("/api/target/{sdbid}")
    def target_api(
        sdbid: str,
        view: str = "actionable",
        priority: str = "",
        role: str = "",
        classification: str = "",
        provider: str = "",
        search: str = "",
        position: int | None = None,
    ):
        try:
            return _workspace(
                context,
                sdbid,
                view=view,
                priority=priority,
                role=role,
                classification=classification,
                provider=provider,
                search=search,
                position=position,
            ).as_dict()
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


def _workspace(
    context: ReviewWebContext,
    sdbid: str,
    *,
    view: str,
    priority: str,
    role: str,
    classification: str,
    provider: str,
    search: str,
    position: int | None,
):
    return build_target_workspace(
        context.session_factory,
        sdbid,
        sample=context.sample,
        filters=queue_filters(
            view=view,
            priority=priority,
            role=role,
            classification=classification,
            provider=provider,
            search=search,
        ),
        position=position,
        catalog_coverage_providers=context.catalog_coverage_providers,
        catalog_update_available=context.catalog_update_factory is not None,
        nearby_import_available=(
            context.identity_service_factory is not None
            and context.catalog_update_factory is not None
        ),
    )
