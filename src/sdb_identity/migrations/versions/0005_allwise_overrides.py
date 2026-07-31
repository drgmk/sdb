"""Durable photometry include/exclude overrides for AllWISE and later catalogs."""

from alembic import op
import sqlalchemy as sa

revision = "0005_allwise_overrides"
down_revision = "0004_batch_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "photometry_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "target_id",
            sa.Integer(),
            sa.ForeignKey("targets.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("band", sa.String(length=30), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_photometry_overrides_target_id",
        "photometry_overrides",
        ["target_id"],
    )
    op.create_index(
        "ix_photometry_overrides_provider",
        "photometry_overrides",
        ["provider"],
    )
    op.create_index(
        "ix_photometry_overrides_band",
        "photometry_overrides",
        ["band"],
    )
    op.execute(
        "CREATE VIEW photometry_override_history AS "
        "SELECT o.id, t.sdbid, o.provider, o.band, o.excluded, o.actor, "
        "o.reason, o.created_at FROM photometry_overrides o "
        "JOIN targets t ON t.id=o.target_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS photometry_override_history")
    op.drop_table("photometry_overrides")
