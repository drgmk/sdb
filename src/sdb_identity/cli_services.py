"""Shared service construction for CLI command modules."""

from __future__ import annotations


def build_update_service(
    sessions,
    reference_database,
    *,
    workers=4,
    bulk_chunk_size=500,
    offline=False,
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
