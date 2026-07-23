"""Durable staged batch ingestion."""

from alembic import op

from sdb_identity.models import Base

revision = "0004_batch_ingestion"
down_revision = "0003_simbad_metadata"
branch_labels = None
depends_on = None


TABLES = ("import_runs", "import_items", "import_jobs")


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in TABLES],
    )
    op.execute(
        "CREATE VIEW import_run_status AS "
        "SELECT r.id AS run_id, r.source_path, r.status, r.item_count, "
        "COUNT(j.id) AS job_count, "
        "SUM(CASE WHEN j.status IN ('succeeded','no_match') THEN 1 ELSE 0 END) AS successful_jobs, "
        "SUM(CASE WHEN j.status IN ('transient_failure','permanent_failure','ambiguous') THEN 1 ELSE 0 END) AS failed_jobs, "
        "r.created_at, r.started_at, r.completed_at "
        "FROM import_runs r LEFT JOIN import_jobs j ON j.run_id=r.id GROUP BY r.id"
    )
    op.execute(
        "CREATE VIEW import_job_status AS "
        "SELECT j.id AS job_id, j.run_id, i.row_number, j.stage, j.status, "
        "j.attempts, j.last_error, i.target_id, j.started_at, j.completed_at "
        "FROM import_jobs j JOIN import_items i ON i.id=j.item_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS import_job_status")
    op.execute("DROP VIEW IF EXISTS import_run_status")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in reversed(TABLES)],
    )
