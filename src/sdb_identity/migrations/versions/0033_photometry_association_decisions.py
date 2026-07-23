"""Add audited photometry association decisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0033_photometry_association_decisions"
down_revision = "0032_simbad_metadata_proper_motion"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(inspect(op.get_bind()).get_table_names())
    if "photometry_association_decisions" not in tables:
        op.create_table(
            "photometry_association_decisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False, index=True),
            sa.Column("measurement_id", sa.Integer(), sa.ForeignKey("normalized_measurements.id"), index=True),
            sa.Column("raw_row_id", sa.Integer(), sa.ForeignKey("raw_catalog_rows.id"), index=True),
            sa.Column("provider", sa.String(40), nullable=False, index=True),
            sa.Column("source_id", sa.String(100), nullable=False, index=True),
            sa.Column("band", sa.String(30), index=True),
            sa.Column("scope", sa.String(40), nullable=False, index=True),
            sa.Column("actor", sa.String(100), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    views = {row[0] for row in op.get_bind().execute(sa.text("SELECT name FROM sqlite_master WHERE type='view'"))}
    if "photometry_association_history" not in views:
        op.execute(
            "CREATE VIEW photometry_association_history AS "
            "SELECT d.id, d.target_id, t.sdbid, d.provider, d.source_id, d.band, "
            "d.scope, d.measurement_id, d.raw_row_id, d.actor, d.reason, d.created_at "
            "FROM photometry_association_decisions d "
            "JOIN targets t ON t.id=d.target_id"
        )


def downgrade():
    views = {row[0] for row in op.get_bind().execute(sa.text("SELECT name FROM sqlite_master WHERE type='view'"))}
    if "photometry_association_history" in views:
        op.execute("DROP VIEW photometry_association_history")
    tables = set(inspect(op.get_bind()).get_table_names())
    if "photometry_association_decisions" in tables:
        op.drop_table("photometry_association_decisions")
