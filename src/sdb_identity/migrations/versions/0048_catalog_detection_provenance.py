"""Store catalogue and table provenance for canonical detections."""

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import quote

from alembic import op
import sqlalchemy as sa


revision = "0048_catalog_detection_provenance"
down_revision = "0047_restore_tdsc_component_scope"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("catalog_detection_provenance"):
        op.create_table(
            "catalog_detection_provenance",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "detection_id",
                sa.Integer(),
                sa.ForeignKey("catalog_detections.id"),
                nullable=False,
            ),
            sa.Column("provenance_key", sa.String(length=64), nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False),
            sa.Column("service", sa.String(length=80)),
            sa.Column("catalog_id", sa.String(length=160)),
            sa.Column("table_id", sa.String(length=200)),
            sa.Column("row_key", sa.String(length=300)),
            sa.Column("source_url", sa.Text()),
            sa.Column("access_url", sa.Text()),
            sa.Column("readme_url", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("detection_id", "provenance_key"),
        )
    indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes(
            "catalog_detection_provenance"
        )
    }
    if "ix_catalog_detection_provenance_detection_id" not in indexes:
        op.create_index(
            "ix_catalog_detection_provenance_detection_id",
            "catalog_detection_provenance",
            ["detection_id"],
        )
    rows = bind.exec_driver_sql(
        "SELECT id, provider, release, payload_json FROM catalog_detections"
    )
    for detection_id, provider, release, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError):
            continue
        provenance = payload.get("_sdb_provenance")
        if not isinstance(provenance, list):
            association = payload.get("_sdb_association")
            association = association if isinstance(association, dict) else {}
            table_id = payload.get("_table") or association.get("query_catalog")
            if not table_id and provider == "gaia_dr3":
                bulk = bind.exec_driver_sql(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM raw_catalog_rows AS raw
                        JOIN catalog_runs AS run ON run.id = raw.run_id
                        WHERE raw.detection_id = ?
                          AND run.batch_request_id IS NOT NULL
                    )
                    """,
                    (detection_id,),
                ).scalar()
                table_id = (
                    "gaiadr3.gaia_source" if bulk else "I/355/gaiadr3"
                )
                association = {
                    **association,
                    "query_service": "Gaia TAP" if bulk else "VizieR",
                }
            if not table_id:
                continue
            catalog_id = (
                "I/259" if provider == "tycho2"
                else str(release).split("@", 1)[0]
            )
            readme_catalog_id = (
                catalog_id.rsplit("/", 1)[0]
                if provider in {"2mass", "allwise", "gaia_dr3"}
                else catalog_id
            )
            provenance = [{
                "role": "native_row",
                "service": association.get("query_service") or "VizieR",
                "catalog_id": catalog_id,
                "table_id": str(table_id),
                "access_url": (
                    "https://gea.esac.esa.int/archive/"
                    if association.get("query_service") == "Gaia TAP"
                    else
                    "https://vizier.cds.unistra.fr/viz-bin/VizieR?-source="
                    f"{quote(str(table_id), safe='')}"
                ),
                "readme_url": (
                    "https://cdsarc.cds.unistra.fr/viz-bin/ReadMe/"
                    f"{quote(readme_catalog_id, safe='/')}"
                ),
            }]
        for item in provenance:
            if not isinstance(item, dict):
                continue
            canonical = json.dumps(
                {
                    key: value for key, value in item.items()
                    if value is not None
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            key = hashlib.sha256(canonical.encode()).hexdigest()
            bind.execute(sa.text(
                """
                INSERT OR IGNORE INTO catalog_detection_provenance
                (detection_id, provenance_key, role, service, catalog_id,
                 table_id, row_key, source_url, access_url, readme_url,
                 created_at)
                VALUES
                (:detection_id, :provenance_key, :role, :service, :catalog_id,
                 :table_id, :row_key, :source_url, :access_url, :readme_url,
                 :created_at)
                """
            ), {
                "detection_id": detection_id,
                "provenance_key": key,
                "role": item.get("role") or "native_row",
                "service": item.get("service"),
                "catalog_id": item.get("catalog_id"),
                "table_id": item.get("table_id"),
                "row_key": item.get("row_key"),
                "source_url": item.get("source_url"),
                "access_url": item.get("access_url"),
                "readme_url": item.get("readme_url"),
                "created_at": datetime.now(timezone.utc),
            })


def downgrade():
    if sa.inspect(op.get_bind()).has_table("catalog_detection_provenance"):
        op.drop_table("catalog_detection_provenance")
