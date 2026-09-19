"""Audited decision routes for the review server."""

from __future__ import annotations

from ..commands import (
    review_catalog_association_command,
    review_detection_command,
    review_eligibility_command,
    review_lifecycle_command,
    review_provider_result_command,
)
from ..context import ReviewWebContext


def register_decision_routes(app: object, context: ReviewWebContext) -> None:
    from fastapi import HTTPException

    def command_error(error: Exception) -> HTTPException:
        return HTTPException(status_code=409, detail=str(error))

    def refresh_cache(
        *,
        targets=(),
        detections=(),
        measurements=(),
    ) -> None:
        if context.dashboard_cache is not None:
            context.dashboard_cache.refresh_related(
                target_references=targets,
                detection_ids=detections,
                measurement_ids=measurements,
            )

    @app.post("/api/decision/preview")
    async def decision_preview(payload: dict[str, object]):
        try:
            return review_detection_command(
                context.session_factory, payload, apply=False
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/decision/apply")
    async def decision_apply(payload: dict[str, object]):
        try:
            value = review_detection_command(
                context.session_factory, payload, apply=True
            )
            refresh_cache(
                targets=(
                    str(payload["scope_target"]),
                    *(str(item) for item in payload.get("contributors", [])),
                ),
                detections=(int(payload["detection_id"]),),
                measurements=(
                    int(item) for item in payload.get("measurement_ids", [])
                ),
            )
            return value
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/lifecycle/preview")
    async def lifecycle_preview(payload: dict[str, object]):
        try:
            return review_lifecycle_command(
                context.session_factory, payload, apply=False
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/lifecycle/apply")
    async def lifecycle_apply(payload: dict[str, object]):
        try:
            value = review_lifecycle_command(
                context.session_factory, payload, apply=True
            )
            refresh_cache(targets=(str(payload["target"]),))
            return value
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/eligibility/preview")
    async def eligibility_preview(payload: dict[str, object]):
        try:
            return review_eligibility_command(
                context.session_factory, payload, apply=False
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/eligibility/apply")
    async def eligibility_apply(payload: dict[str, object]):
        try:
            value = review_eligibility_command(
                context.session_factory, payload, apply=True
            )
            refresh_cache(
                targets=(str(payload["target"]),) if payload.get("target") else (),
                measurements=(
                    int(item["measurement_id"])
                    for item in payload.get("changes", [])
                ),
            )
            return value
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/provider-result/preview")
    async def provider_result_preview(payload: dict[str, object]):
        try:
            return review_provider_result_command(
                context.session_factory,
                context.catalog_service_factory,
                payload,
                apply=False,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/provider-result/apply")
    async def provider_result_apply(payload: dict[str, object]):
        try:
            value = review_provider_result_command(
                context.session_factory,
                context.catalog_service_factory,
                payload,
                apply=True,
            )
            candidate = value.get("candidate") or {}
            refresh_cache(
                targets=(str(value["target"]["sdbid"]),),
                detections=(
                    int(candidate["detection_id"]),
                ) if candidate.get("detection_id") is not None else (),
            )
            return value
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/catalog-association/preview")
    async def catalog_association_preview(payload: dict[str, object]):
        try:
            return review_catalog_association_command(
                context.session_factory, payload, apply=False
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error

    @app.post("/api/catalog-association/apply")
    async def catalog_association_apply(payload: dict[str, object]):
        try:
            value = review_catalog_association_command(
                context.session_factory, payload, apply=True
            )
            refresh_cache(
                targets=(str(payload["target"]),),
                detections=(int(payload["detection_id"]),),
            )
            return value
        except (KeyError, ValueError, RuntimeError) as error:
            raise command_error(error) from error
