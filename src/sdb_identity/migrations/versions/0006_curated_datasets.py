"""Revisioned curated datasets and affected-target tracking."""

from alembic import op

from sdb_identity.models import Base

revision = "0006_curated_datasets"
down_revision = "0005_allwise_overrides"
branch_labels = None
depends_on = None


TABLES = ("dataset_revisions", "curated_records", "dataset_dirty_targets")


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in TABLES],
    )
    op.execute(
        "CREATE VIEW unresolved_curated_records AS "
        "SELECT d.dataset, d.id AS revision_id, r.record_no, r.source_identifier, "
        "r.association_status, r.association_message "
        "FROM curated_records r JOIN dataset_revisions d ON d.id=r.revision_id "
        "WHERE d.is_current=1 AND r.association_status != 'matched'"
    )
    op.execute(
        "CREATE VIEW pending_dataset_exports AS "
        "SELECT d.dataset, q.revision_id, t.sdbid, q.reason "
        "FROM dataset_dirty_targets q JOIN dataset_revisions d ON d.id=q.revision_id "
        "JOIN targets t ON t.id=q.target_id WHERE q.exported_at IS NULL"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS pending_dataset_exports")
    op.execute("DROP VIEW IF EXISTS unresolved_curated_records")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in reversed(TABLES)],
    )
