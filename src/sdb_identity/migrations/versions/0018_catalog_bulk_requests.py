"""Bulk catalog request provenance."""

import sqlalchemy as sa
from alembic import op

from sdb_identity.models import Base


revision = "0018_catalog_bulk_requests"
down_revision = "0017_sample_exports"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("catalog_runs")
    }
    Base.metadata.create_all(
        bind=bind,
        tables=[Base.metadata.tables["catalog_batch_requests"]],
    )
    if "batch_request_id" not in existing_columns:
        # SQLite supports direct ADD COLUMN with a nullable REFERENCES clause.
        # Avoid batch table reconstruction because several existing views depend
        # on catalog_runs and make the temporary-table rename invalid.
        op.execute(
            "ALTER TABLE catalog_runs ADD COLUMN batch_request_id INTEGER "
            "REFERENCES catalog_batch_requests(id)"
        )
        op.create_index(
            "ix_catalog_runs_batch_request_id", "catalog_runs", ["batch_request_id"]
        )
    op.execute(
        "CREATE VIEW catalog_batch_status AS "
        "SELECT b.id AS batch_request_id, b.provider, b.release, b.target_count, "
        "b.chunk_size, b.status, b.error, b.started_at, b.completed_at, "
        "COUNT(r.id) AS run_count FROM catalog_batch_requests b "
        "LEFT JOIN catalog_runs r ON r.batch_request_id=b.id GROUP BY b.id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS catalog_batch_status")
    op.drop_index("ix_catalog_runs_batch_request_id", table_name="catalog_runs")
    op.drop_column("catalog_runs", "batch_request_id")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["catalog_batch_requests"]],
    )
