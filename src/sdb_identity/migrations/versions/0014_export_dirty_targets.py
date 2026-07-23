"""Unified export dirty-target events."""

from alembic import op

from sdb_identity.models import Base


revision = "0014_export_dirty_targets"
down_revision = "0013_iras_detection_families"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["export_dirty_targets"]],
    )
    op.execute(
        "CREATE VIEW pending_export_targets AS "
        "SELECT t.id AS target_id, t.sdbid, COUNT(d.id) AS event_count, "
        "MIN(d.created_at) AS dirty_since "
        "FROM targets t JOIN export_dirty_targets d ON d.target_id=t.id "
        "WHERE d.exported_at IS NULL GROUP BY t.id, t.sdbid"
    )
    op.execute(
        "INSERT INTO export_dirty_targets "
        "(target_id, source_type, source_id, reason, created_at) "
        "SELECT target_id, 'dataset', CAST(revision_id AS TEXT), reason, CURRENT_TIMESTAMP "
        "FROM dataset_dirty_targets WHERE exported_at IS NULL"
    )
    op.execute(
        "INSERT INTO export_dirty_targets "
        "(target_id, source_type, source_id, reason, created_at) "
        "SELECT target_id, 'reference', CAST(application_run_id AS TEXT), reason, CURRENT_TIMESTAMP "
        "FROM reference_dirty_targets WHERE exported_at IS NULL"
    )
    op.execute(
        "INSERT INTO export_dirty_targets "
        "(target_id, source_type, source_id, reason, created_at) "
        "SELECT target_id, 'catalog_override', CAST(override_id AS TEXT), reason, CURRENT_TIMESTAMP "
        "FROM catalog_dirty_targets WHERE exported_at IS NULL"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS pending_export_targets")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["export_dirty_targets"]],
    )
