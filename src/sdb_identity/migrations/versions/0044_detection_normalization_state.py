"""Track normalization independently for canonical catalog detections."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0044_detection_normalization_state"
down_revision = "0043_catalog_result_actions"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("catalog_detections")
    }
    with op.batch_alter_table("catalog_detections") as batch:
        if "normalization_status" not in columns:
            batch.add_column(sa.Column(
                "normalization_status",
                sa.String(30),
                nullable=False,
                server_default="pending",
            ))
        if "normalization_error" not in columns:
            batch.add_column(sa.Column("normalization_error", sa.Text()))
        if "normalized_at" not in columns:
            batch.add_column(sa.Column(
                "normalized_at", sa.DateTime(timezone=True),
            ))
    indexes = {
        index["name"]
        for index in inspect(op.get_bind()).get_indexes("catalog_detections")
    }
    if "ix_catalog_detections_normalization_status" not in indexes:
        op.create_index(
            "ix_catalog_detections_normalization_status",
            "catalog_detections",
            ["normalization_status"],
        )
    op.execute(
        "UPDATE catalog_detections SET normalization_status='completed', "
        "normalized_at=CURRENT_TIMESTAMP WHERE id IN ("
        "SELECT DISTINCT detection_id FROM normalized_measurements)"
    )


def downgrade():
    with op.batch_alter_table("catalog_detections") as batch:
        batch.drop_index("ix_catalog_detections_normalization_status")
        batch.drop_column("normalized_at")
        batch.drop_column("normalization_error")
        batch.drop_column("normalization_status")
