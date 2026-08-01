"""Import and catalog-coverage routes for the review server."""

from __future__ import annotations

from ..import_commands import (
    apply_nearby_import_command,
    review_catalog_coverage_command,
    review_relatives_command,
    search_nearby_import_command,
)
from ..context import ReviewWebContext


def register_import_routes(app: object, context: ReviewWebContext) -> None:
    from fastapi import HTTPException

    def command_error(error: Exception) -> HTTPException:
        return HTTPException(status_code=409, detail=str(error))

    @app.post("/api/relatives/preview")
    async def relatives_preview(payload: dict[str, object]):
        try:
            return review_relatives_command(
                context.session_factory,
                context.identity_service_factory,
                payload,
                apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/relatives/apply")
    async def relatives_apply(payload: dict[str, object]):
        try:
            return review_relatives_command(
                context.session_factory,
                context.identity_service_factory,
                payload,
                apply=True,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/catalog-coverage/preview")
    async def catalog_coverage_preview(payload: dict[str, object]):
        try:
            return review_catalog_coverage_command(
                context.session_factory,
                context.catalog_coverage_providers,
                context.catalog_update_factory,
                context.catalog_service_factory,
                payload,
                apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/catalog-coverage/apply")
    async def catalog_coverage_apply(payload: dict[str, object]):
        try:
            return review_catalog_coverage_command(
                context.session_factory,
                context.catalog_coverage_providers,
                context.catalog_update_factory,
                context.catalog_service_factory,
                payload,
                apply=True,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/nearby-import/search")
    async def nearby_import_search(payload: dict[str, object]):
        try:
            return search_nearby_import_command(
                context.session_factory,
                context.identity_service_factory,
                payload,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/nearby-import/apply")
    async def nearby_import_apply(payload: dict[str, object]):
        try:
            return apply_nearby_import_command(
                context.session_factory,
                context.identity_service_factory,
                context.catalog_update_factory,
                context.catalog_coverage_providers,
                payload,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error
