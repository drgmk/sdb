"""Append-only manual catalog candidate selections."""

from alembic import op

from sdb_identity.models import Base

revision = "0009_catalog_match_overrides"
down_revision = "0008_reference_application"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["catalog_match_overrides"],
            Base.metadata.tables["catalog_dirty_targets"],
        ],
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
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["catalog_dirty_targets"],
            Base.metadata.tables["catalog_match_overrides"],
        ],
    )
