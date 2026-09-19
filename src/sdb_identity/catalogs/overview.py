"""Read-only catalog-provider overview projection."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from .registry import CATALOG_PROVIDERS
from .provenance import vizier_access_url
from ..models.catalogs import CatalogRun, NormalizedMeasurement
from ..reference.store import ReferenceStore

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


def catalog_overview(
    reference_store: ReferenceStore | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> dict[str, object]:
    """Describe provider policy, reference availability, and stored SDB data.

    Reference snapshots are inputs used for future matching.  Catalog runs and
    normalized measurements have already been copied into the main SDB
    database and remain usable when the configured reference database is not
    available.  Reporting both avoids presenting those two states as if they
    were the same thing.
    """
    stored = _stored_catalog_state(session_factory)
    providers = []
    for definition in CATALOG_PROVIDERS.values():
        row = asdict(definition)
        row["bands"] = [
            {"name": name, "wavelength_micron": wavelength}
            for name, wavelength in definition.bands
        ]
        row["vizier_url"] = vizier_access_url(definition.catalog)
        row["snapshot"] = None
        row["status"] = "remote"
        row["retained_tables"] = []
        row["stored"] = stored.get(definition.key, {
            "current_results": 0,
            "measurements": 0,
            "releases": [],
        })
        if definition.is_snapshot:
            row["status"] = "missing"
            if reference_store is not None:
                snapshot = reference_store.current_snapshot(definition.key)
                if snapshot is not None:
                    tables = reference_store.describe(
                        snapshot.id, adapter=definition.key,
                    )
                    science = set(definition.science_tables)
                    row["status"] = "current"
                    row["snapshot"] = {
                        "content_sha256": snapshot.content_sha256,
                        "source_url": snapshot.source_url,
                        "retrieved_at": snapshot.retrieved_at.isoformat(),
                        "row_count": sum(int(table["row_count"]) for table in tables),
                        "tables": [
                            {
                                "name": table["name"],
                                "description": table["description"],
                                "row_count": table["row_count"],
                                "science": table["name"] in science,
                            }
                            for table in tables
                        ],
                    }
                    row["retained_tables"] = [
                        table["name"] for table in tables
                        if table["name"] not in science
                    ]
        providers.append(row)
    return {
        "provider_count": len(providers),
        "remote_count": sum(row["status"] == "remote" for row in providers),
        "snapshot_current_count": sum(row["status"] == "current" for row in providers),
        "snapshot_missing_count": sum(row["status"] == "missing" for row in providers),
        "reference_database": (
            None if reference_store is None else str(reference_store.path)
        ),
        "stored_current_result_count": sum(
            int(row["stored"]["current_results"]) for row in providers
        ),
        "stored_measurement_count": sum(
            int(row["stored"]["measurements"]) for row in providers
        ),
        "providers": providers,
    }


def _stored_catalog_state(
    session_factory: sessionmaker[Session] | None,
) -> dict[str, dict[str, object]]:
    if session_factory is None:
        return {}
    with session_factory() as session:
        run_rows = session.execute(
            select(
                CatalogRun.provider,
                func.count(CatalogRun.id),
            )
            .where(CatalogRun.is_current.is_(True))
            .group_by(CatalogRun.provider)
        )
        measurement_rows = session.execute(
            select(
                NormalizedMeasurement.provider,
                func.count(NormalizedMeasurement.id),
            ).group_by(NormalizedMeasurement.provider)
        )
        release_rows = session.execute(
            select(CatalogRun.provider, CatalogRun.release)
            .where(CatalogRun.is_current.is_(True))
            .distinct()
            .order_by(CatalogRun.provider, CatalogRun.release)
        )
        result: dict[str, dict[str, object]] = {}
        for provider, count in run_rows:
            result.setdefault(provider, {})["current_results"] = int(count)
        for provider, count in measurement_rows:
            result.setdefault(provider, {})["measurements"] = int(count)
        for provider, release in release_rows:
            result.setdefault(provider, {}).setdefault("releases", []).append(release)
    for value in result.values():
        value.setdefault("current_results", 0)
        value.setdefault("measurements", 0)
        value.setdefault("releases", [])
    return result
