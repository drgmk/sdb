"""Durable photometry include/exclude overrides for AllWISE and later catalogs."""

from alembic import op

from sdb_identity.models import Base

revision = "0005_allwise_overrides"
down_revision = "0004_batch_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["photometry_overrides"]],
    )
    op.execute(
        "CREATE VIEW photometry_override_history AS "
        "SELECT o.id, t.sdbid, o.provider, o.band, o.excluded, o.actor, "
        "o.reason, o.created_at FROM photometry_overrides o "
        "JOIN targets t ON t.id=o.target_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS photometry_override_history")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["photometry_overrides"]],
    )
