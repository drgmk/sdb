"""Append-only manual catalog candidate selections."""

from alembic import op
import sqlalchemy as sa

from sdb_identity.models import Base

revision = "0009_catalog_match_overrides"
down_revision = "0008_reference_application"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["catalog_match_overrides"]],
    )
    # catalog_dirty_targets is created inline (its model was retired once
    # export_dirty_targets subsumed it).
    op.create_table(
        "catalog_dirty_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("override_id", sa.Integer(), sa.ForeignKey("catalog_match_overrides.id"), nullable=False, unique=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False, index=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("exported_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "CREATE VIEW pending_catalog_exports AS "
        "SELECT q.id, t.sdbid, o.provider, q.reason FROM catalog_dirty_targets q "
        "JOIN catalog_match_overrides o ON o.id=q.override_id "
        "JOIN targets t ON t.id=q.target_id WHERE q.exported_at IS NULL"
    )
    op.execute(
        "CREATE VIEW catalog_match_override_history AS "
        "SELECT o.id, t.sdbid, o.provider, o.previous_run_id, "
        "o.replacement_run_id, o.selected_source_id, o.actor, o.reason, "
        "o.created_at FROM catalog_match_overrides o "
        "JOIN targets t ON t.id=o.target_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS pending_catalog_exports")
    op.execute("DROP VIEW IF EXISTS catalog_match_override_history")
    op.drop_table("catalog_dirty_targets")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["catalog_match_overrides"]],
    )
