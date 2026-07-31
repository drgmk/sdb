"""Append-only curated associations and record-level photometry overrides."""

from alembic import op
import sqlalchemy as sa

from sdb_identity.models import Base

revision = "0007_curated_controls"
down_revision = "0006_curated_datasets"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["curated_association_actions"]],
    )
    op.create_table(
        "curated_photometry_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset", sa.String(length=40), nullable=False),
        sa.Column("record_no", sa.Integer(), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_curated_photometry_overrides_dataset",
        "curated_photometry_overrides",
        ["dataset"],
    )
    op.create_index(
        "ix_curated_photometry_overrides_record_no",
        "curated_photometry_overrides",
        ["record_no"],
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
    op.drop_table("curated_photometry_overrides")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["curated_association_actions"]],
    )
