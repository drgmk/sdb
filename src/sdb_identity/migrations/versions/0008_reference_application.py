"""Durable bulk application of whole-catalog reference snapshots."""

from alembic import op

from sdb_identity.models import Base

revision = "0008_reference_application"
down_revision = "0007_curated_controls"
branch_labels = None
depends_on = None

TABLES = (
    "reference_application_runs",
    "reference_application_items",
    "reference_application_records",
    "reference_dirty_targets",
)


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in TABLES],
    )
    op.execute(
        "CREATE VIEW reference_application_status AS "
        "SELECT id, provider, snapshot_sha256, status, target_count, "
        "refreshed_count, match_count, ambiguous_count, no_match_count, "
        "row_count, unmatched_row_count, created_at, completed_at "
        "FROM reference_application_runs"
    )
    op.execute(
        "CREATE VIEW unmatched_reference_records AS "
        "SELECT a.provider, a.snapshot_sha256, r.source_identifier, r.row_sha256 "
        "FROM reference_application_records r JOIN reference_application_runs a "
        "ON a.id=r.application_run_id WHERE r.status='unmatched'"
    )
    op.execute(
        "CREATE VIEW pending_reference_exports AS "
        "SELECT a.provider, q.application_run_id, t.sdbid, q.reason "
        "FROM reference_dirty_targets q JOIN reference_application_runs a "
        "ON a.id=q.application_run_id JOIN targets t ON t.id=q.target_id "
        "WHERE q.exported_at IS NULL"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS pending_reference_exports")
    op.execute("DROP VIEW IF EXISTS unmatched_reference_records")
    op.execute("DROP VIEW IF EXISTS reference_application_status")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in reversed(TABLES)],
    )
