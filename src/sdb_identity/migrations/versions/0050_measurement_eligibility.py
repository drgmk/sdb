"""Replace broad photometry overrides with measurement eligibility actions."""

from alembic import op
import sqlalchemy as sa


revision = "0050_measurement_eligibility"
down_revision = "0049_catalog_provenance_locator"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("measurement_eligibility_actions"):
        op.create_table(
            "measurement_eligibility_actions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "measurement_id",
                sa.Integer(),
                sa.ForeignKey("normalized_measurements.id"),
                nullable=False,
            ),
            sa.Column("excluded", sa.Boolean(), nullable=False),
            sa.Column("actor", sa.String(length=100), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_measurement_eligibility_actions_measurement_id",
            "measurement_eligibility_actions",
            ["measurement_id"],
        )
    op.execute(
        "CREATE VIEW IF NOT EXISTS measurement_eligibility_history AS "
        "SELECT action.id, action.measurement_id, measurement.provider, "
        "measurement.source_id, measurement.band, action.excluded, "
        "action.actor, action.reason, action.created_at "
        "FROM measurement_eligibility_actions AS action "
        "JOIN normalized_measurements AS measurement "
        "ON measurement.id = action.measurement_id"
    )
    op.execute("DROP VIEW IF EXISTS photometry_override_history")
    op.execute("DROP VIEW IF EXISTS curated_photometry_override_history")
    inspector = sa.inspect(op.get_bind())
    for table in ("photometry_overrides", "curated_photometry_overrides"):
        if inspector.has_table(table):
            op.drop_table(table)


def downgrade():
    raise RuntimeError("development schema migrations are forward-only")
