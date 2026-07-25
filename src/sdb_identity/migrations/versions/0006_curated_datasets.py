"""Revisioned curated datasets and affected-target tracking."""

from alembic import op
import sqlalchemy as sa

from sdb_identity.models import Base

revision = "0006_curated_datasets"
down_revision = "0005_allwise_overrides"
branch_labels = None
depends_on = None


# dataset_dirty_targets is created inline (its model was retired once
# export_dirty_targets subsumed it); the rest still come from live metadata.
TABLES = ("dataset_revisions", "curated_records")


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in TABLES],
    )
    op.create_table(
        "dataset_dirty_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision_id", sa.Integer(), sa.ForeignKey("dataset_revisions.id"), nullable=False, index=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False, index=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("exported_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("revision_id", "target_id"),
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
    op.drop_table("dataset_dirty_targets")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in reversed(TABLES)],
    )
