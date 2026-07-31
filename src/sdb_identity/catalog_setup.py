"""Construct catalog services consistently for CLI and local review actions."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from .catalog_registry import (
    REMOTE_CATALOG_PROVIDERS,
    build_catalog_adapter,
)
from .catalog_acquisition import CatalogAcquisitionService
from .catalog_decisions import CatalogDecisionService
from .catalog_normalization import CatalogNormalizationService


def catalog_operator_service_for_provider(
    sessions: sessionmaker[Session],
    provider: str,
    *,
    reference_database: str | Path,
    offline: bool,
    action: str = "accept_candidate",
) -> CatalogDecisionService | CatalogNormalizationService:
    if offline and action == "retry" and provider in REMOTE_CATALOG_PROVIDERS:
        raise ValueError(
            f"{provider} retry is unavailable while the review server is offline"
        )
    from .reference import ReferenceStore

    adapter = build_catalog_adapter(
        provider, reference_store=ReferenceStore(reference_database),
    )
    if action == "normalize":
        return CatalogNormalizationService(sessions, {provider: adapter})
    acquisition = CatalogAcquisitionService(sessions, {provider: adapter})
    return CatalogDecisionService(
        sessions, {provider: adapter}, acquisition=acquisition,
    )
