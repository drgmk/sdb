"""Read-only catalog-provider overview projection."""

from __future__ import annotations

from dataclasses import asdict

from .registry import CATALOG_PROVIDERS
from .provenance import vizier_access_url
from ..reference.store import ReferenceStore


def catalog_overview(reference_store: ReferenceStore | None = None) -> dict[str, object]:
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
        "providers": providers,
    }
