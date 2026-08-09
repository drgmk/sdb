from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


FIT_PACKAGE_SCHEMA_NAME = "sdb-fit-package"
FIT_PACKAGE_SCHEMA_VERSION = 1


def write_fit_package_manifest(
    output: str | Path,
    *,
    package_id: str,
    directory_name: str,
    primary_sdbid: str,
    selected_sdbids: Iterable[str],
    graph: Mapping[str, object],
    inputs: Iterable[Mapping[str, object]],
    database_revision: str,
    generated_at: datetime | None = None,
) -> Path:
    """Write the portable manifest stored beside one set of SDF inputs."""
    output = Path(output)
    timestamp = generated_at or datetime.now(timezone.utc)
    input_rows = sorted(
        (dict(row) for row in inputs),
        key=lambda row: (str(row.get("sdbid", "")), str(row.get("file", ""))),
    )
    payload = {
        "schema": FIT_PACKAGE_SCHEMA_NAME,
        "schema_version": FIT_PACKAGE_SCHEMA_VERSION,
        "generated_at": timestamp.isoformat(),
        "database_revision": database_revision,
        "package": {
            "package_id": package_id,
            "directory_name": directory_name,
            "primary_sdbid": primary_sdbid,
            "selected_sdbids": sorted(selected_sdbids),
            "input_sdbids": [row["sdbid"] for row in input_rows],
        },
        "inputs": input_rows,
        "graph": dict(graph),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output
