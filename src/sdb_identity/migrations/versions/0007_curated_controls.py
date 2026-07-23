"""Append-only curated associations and record-level photometry overrides."""

from alembic import op

from sdb_identity.models import Base

revision = "0007_curated_controls"
down_revision = "0006_curated_datasets"
branch_labels = None
depends_on = None


TABLES = ("curated_association_actions", "curated_photometry_overrides")


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in TABLES],
    )
    op.execute(
        "CREATE VIEW curated_association_history AS "
        "SELECT a.id, a.dataset, a.record_no, a.action, t.sdbid, a.actor, "
        "a.reason, a.created_at FROM curated_association_actions a "
        "LEFT JOIN targets t ON t.id=a.target_id"
    )
    op.execute(
        "CREATE VIEW curated_photometry_override_history AS "
        "SELECT id, dataset, record_no, excluded, actor, reason, created_at "
        "FROM curated_photometry_overrides"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS curated_photometry_override_history")
    op.execute("DROP VIEW IF EXISTS curated_association_history")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in reversed(TABLES)],
    )
