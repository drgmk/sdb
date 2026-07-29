"""Add provider-native row locators to catalogue provenance."""

import hashlib
import json
from urllib.parse import quote

from alembic import op
import sqlalchemy as sa


revision = "0049_catalog_provenance_locator"
down_revision = "0048_catalog_detection_provenance"
branch_labels = None
depends_on = None


IDENTIFIERS = {
    "2mass": ("_2MASS", ("_2MASS", "2MASS", "designation")),
    "allwise": ("AllWISE", ("AllWISE", "designation")),
    "gaia_dr3": ("Source", ("Source", "source_id")),
    "gaspar13": ("Name", ("Name",)),
    "hip2": ("HIP", ("HIP",)),
    "iras_fsc": ("IRAS", ("IRAS",)),
    "iras_psc": ("IRAS", ("IRAS",)),
    "koen10": ("HIP", ("HIP",)),
    "tdsc": ("TDSC", ("TDSC",)),
    "ubvmeans": ("SimbadName", ("SimbadName",)),
    "v70a": ("Name", ("Name",)),
}


def _text(value):
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text or None


def _entry_url(table_id, column, value):
    return (
        "https://vizier.cds.unistra.fr/viz-bin/VizieR-5?"
        f"-out.add=.&-source={quote(table_id, safe='')}"
        f"&{quote(column, safe='')}==={quote(value, safe='')}"
    )


def _key(values):
    canonical = json.dumps(
        {
            name: value for name, value in values.items()
            if value is not None
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("catalog_detection_provenance"):
        return
    columns = {
        column["name"] for column in inspector.get_columns(
            "catalog_detection_provenance"
        )
    }
    with op.batch_alter_table("catalog_detection_provenance") as batch:
        if "identifier_column" not in columns:
            batch.add_column(sa.Column("identifier_column", sa.String(160)))
        if "identifier_value" not in columns:
            batch.add_column(sa.Column("identifier_value", sa.String(300)))

    rows = bind.execute(sa.text(
        """
        SELECT provenance.id, provenance.detection_id, detection.provider,
               detection.source_id, detection.payload_json,
               provenance.role, provenance.service, provenance.catalog_id,
               provenance.table_id, provenance.row_key,
               provenance.source_url, provenance.access_url,
               provenance.readme_url
        FROM catalog_detection_provenance AS provenance
        JOIN catalog_detections AS detection
          ON detection.id = provenance.detection_id
        ORDER BY provenance.id
        """
    )).mappings()
    for row in rows:
        locator = IDENTIFIERS.get(row["provider"])
        if locator is None:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        column, payload_columns = locator
        value = next(
            (
                candidate for name in payload_columns
                if (candidate := _text(payload.get(name))) is not None
            ),
            None,
        )
        if value is None and row["provider"] in {
            "2mass", "allwise", "gaia_dr3"
        }:
            value = _text(row["source_id"])
        if value is None:
            continue
        table_id = (
            "I/355/gaiadr3"
            if row["provider"] == "gaia_dr3"
            else row["table_id"]
        )
        if not table_id:
            continue
        values = {
            "role": row["role"],
            "service": row["service"],
            "catalog_id": row["catalog_id"],
            "table_id": table_id,
            "row_key": row["row_key"],
            "identifier_column": column,
            "identifier_value": value,
            "source_url": row["source_url"],
            "access_url": _entry_url(table_id, column, value),
            "readme_url": row["readme_url"],
        }
        provenance_key = _key(values)
        duplicate_id = bind.execute(sa.text(
            """
            SELECT id
            FROM catalog_detection_provenance
            WHERE detection_id = :detection_id
              AND provenance_key = :provenance_key
              AND id <> :id
            """
        ), {
            "detection_id": row["detection_id"],
            "provenance_key": provenance_key,
            "id": row["id"],
        }).scalar()
        if duplicate_id is not None:
            bind.execute(sa.text(
                "DELETE FROM catalog_detection_provenance WHERE id = :id"
            ), {"id": row["id"]})
            continue
        bind.execute(sa.text(
            """
            UPDATE catalog_detection_provenance
            SET provenance_key = :provenance_key,
                table_id = :table_id,
                identifier_column = :identifier_column,
                identifier_value = :identifier_value,
                access_url = :access_url
            WHERE id = :id
            """
        ), {
            "id": row["id"],
            "provenance_key": provenance_key,
            "table_id": table_id,
            "identifier_column": column,
            "identifier_value": value,
            "access_url": values["access_url"],
        })


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("catalog_detection_provenance"):
        return
    columns = {
        column["name"] for column in inspector.get_columns(
            "catalog_detection_provenance"
        )
    }
    with op.batch_alter_table("catalog_detection_provenance") as batch:
        if "identifier_value" in columns:
            batch.drop_column("identifier_value")
        if "identifier_column" in columns:
            batch.drop_column("identifier_column")
