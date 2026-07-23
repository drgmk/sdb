from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .fitting_groups import fitting_group_report


SCHEMA_NAME = "sdb-joint-fit-manifest"
SCHEMA_VERSION = 1


def write_joint_fit_manifest(
    session_factory: sessionmaker[Session],
    output: str | Path,
    *,
    target_reference: str | int | None = None,
    sample: str | None = None,
    legacy_exports: Iterable[Mapping[str, object]] = (),
    generated_at: datetime | None = None,
    database_revision: str | None = None,
) -> Path:
    """Write an atomic, versioned sidecar describing current joint-fit inputs."""
    output = Path(output)
    graph = fitting_group_report(
        session_factory,
        target_reference=target_reference,
        sample=sample,
    )
    if database_revision is None:
        with session_factory() as session:
            database_revision = session.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one()
    timestamp = generated_at or datetime.now(timezone.utc)
    exports = sorted(
        (dict(row) for row in legacy_exports),
        key=lambda row: (str(row.get("sdbid", "")), str(row.get("output", ""))),
    )
    selected_sdbids = graph["selection"]["selected_sdbids"]
    if len(exports) == 1 and len(selected_sdbids) == 1 and not exports[0].get("sdbid"):
        exports[0]["sdbid"] = selected_sdbids[0]
    payload = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp.isoformat(),
        "database_revision": database_revision,
        "legacy_exports": exports,
        "graph": graph,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def target_manifest_path(rawphot_path: str | Path) -> Path:
    """Return the conventional joint-fit sidecar path for one rawphot file."""
    path = Path(rawphot_path)
    stem = path.stem
    if stem.endswith("-rawphot"):
        stem = stem[:-len("-rawphot")]
    return path.with_name(f"{stem}-joint-fit.json")
