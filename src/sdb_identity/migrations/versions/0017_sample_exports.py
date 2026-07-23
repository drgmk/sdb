"""Durable sample export runs and per-target manifests."""

from alembic import op

from sdb_identity.models import Base


revision = "0017_sample_exports"
down_revision = "0016_samples"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["sample_export_runs"],
            Base.metadata.tables["sample_export_items"],
        ],
    )
    op.execute(
        "CREATE VIEW sample_export_summary AS "
        "SELECT r.id AS run_id, r.sample_id, s.name AS sample_name, r.output_dir, "
        "r.database_revision, r.status, r.manifest_path, r.started_at, r.completed_at, "
        "COUNT(i.id) AS target_count, "
        "SUM(CASE WHEN i.status='exported' THEN 1 ELSE 0 END) AS exported_count, "
        "SUM(CASE WHEN i.status='skipped' THEN 1 ELSE 0 END) AS skipped_count, "
        "SUM(CASE WHEN i.status='failed' THEN 1 ELSE 0 END) AS failed_count "
        "FROM sample_export_runs r JOIN samples s ON s.id=r.sample_id "
        "LEFT JOIN sample_export_items i ON i.run_id=r.id GROUP BY r.id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS sample_export_summary")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["sample_export_items"],
            Base.metadata.tables["sample_export_runs"],
        ],
    )
