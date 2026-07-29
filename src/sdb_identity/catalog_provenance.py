from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import quote


@dataclass(frozen=True)
class CatalogProvenance:
    """The provider location from which a canonical detection was obtained."""

    role: str = "native_row"
    service: str | None = None
    catalog_id: str | None = None
    table_id: str | None = None
    row_key: str | None = None
    identifier_column: str | None = None
    identifier_value: str | None = None
    source_url: str | None = None
    access_url: str | None = None
    readme_url: str | None = None

    @property
    def key(self) -> str:
        canonical = json.dumps(
            self.as_payload(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def as_payload(self) -> dict[str, str]:
        return {
            key: value for key, value in asdict(self).items()
            if value is not None
        }


def vizier_access_url(table_id: str) -> str:
    return (
        "https://vizier.cds.unistra.fr/viz-bin/VizieR?-source="
        f"{quote(table_id, safe='')}"
    )


def vizier_entry_url(
    table_id: str,
    identifier_column: str,
    identifier_value: str,
) -> str:
    """Return a VizieR table view filtered to a provider-native identifier."""

    return (
        "https://vizier.cds.unistra.fr/viz-bin/VizieR-5?"
        f"-out.add=.&-source={quote(table_id, safe='')}"
        f"&{quote(identifier_column, safe='')}==="
        f"{quote(identifier_value, safe='')}"
    )


def vizier_readme_url(catalog_id: str) -> str:
    return (
        "https://cdsarc.cds.unistra.fr/viz-bin/ReadMe/"
        f"{quote(catalog_id, safe='/')}"
    )


def with_payload_provenance(
    payload: Mapping[str, object],
    provenance: Iterable[CatalogProvenance],
) -> dict[str, object]:
    result = dict(payload)
    values = tuple(provenance)
    if values:
        result["_sdb_provenance"] = [item.as_payload() for item in values]
    return result


def provenance_from_payload(
    payload: Mapping[str, object],
) -> tuple[CatalogProvenance, ...]:
    raw = payload.get("_sdb_provenance")
    if not isinstance(raw, list):
        return ()
    result = []
    fields = set(CatalogProvenance.__dataclass_fields__)
    for item in raw:
        if not isinstance(item, dict):
            continue
        values = {
            key: str(value)
            for key, value in item.items()
            if key in fields and value is not None
        }
        result.append(CatalogProvenance(**values))
    return tuple(result)


def materialize_catalog_documentation(
    database_path: str | Path,
    *,
    provider: str,
    catalog_id: str,
    release: str,
    content_sha256: str,
    source_url: str,
    readme: str,
    tables: Iterable[tuple[str, int]],
) -> Path:
    """Write the provider ReadMe and a small manifest beside a catalogue DB."""

    database = Path(database_path).expanduser().resolve()
    root = database.parent / f"{database.name}.catalogs"

    def safe_part(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("_") or "catalog"

    destination = root / safe_part(provider) / safe_part(catalog_id)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "ReadMe").write_text(readme, encoding="utf-8")
    manifest = {
        "provider": provider,
        "catalog_id": catalog_id,
        "release": release,
        "content_sha256": content_sha256,
        "source_url": source_url,
        "readme": "ReadMe",
        "tables": [
            {"name": name, "row_count": row_count}
            for name, row_count in tables
        ],
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination
