"""Add target lifecycle and audited measurement contributors."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0035_system_photometry_foundation"
down_revision = "0034_hierarchy_lookup_indexes"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(inspect(op.get_bind()).get_table_names())
    if "target_lifecycle_actions" not in tables:
        op.create_table(
            "target_lifecycle_actions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False, index=True),
            sa.Column("role", sa.String(30), nullable=False, index=True),
            sa.Column("state", sa.String(30), nullable=False, index=True),
            sa.Column("superseded_by_target_id", sa.Integer(), sa.ForeignKey("targets.id"), index=True),
            sa.Column("actor", sa.String(100), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "measurement_association_actions" not in tables:
        op.create_table(
            "measurement_association_actions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("measurement_id", sa.Integer(), sa.ForeignKey("normalized_measurements.id"), nullable=False, index=True),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False, index=True),
            sa.Column("action", sa.String(20), nullable=False, index=True),
            sa.Column("role", sa.String(40), nullable=False),
            sa.Column("method", sa.String(40), nullable=False),
            sa.Column("weight", sa.Float()),
            sa.Column("actor", sa.String(100), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    op.execute("DROP VIEW IF EXISTS current_target_lifecycle")
    op.execute(
        "CREATE VIEW current_target_lifecycle AS "
        "SELECT t.id AS target_id, t.sdbid, "
        "COALESCE(a.role, 'unspecified') AS role, "
        "COALESCE(a.state, 'active') AS state, "
        "a.superseded_by_target_id, s.sdbid AS superseded_by_sdbid, "
        "a.actor, a.reason, a.created_at "
        "FROM targets t "
        "LEFT JOIN target_lifecycle_actions a ON a.id=("
        "SELECT MAX(a2.id) FROM target_lifecycle_actions a2 WHERE a2.target_id=t.id) "
        "LEFT JOIN targets s ON s.id=a.superseded_by_target_id"
    )
    op.execute("DROP VIEW IF EXISTS measurement_assignment_history")
    op.execute(
        "CREATE VIEW measurement_assignment_history AS "
        "SELECT a.id, a.measurement_id, a.target_id, t.sdbid, a.action, a.role, "
        "a.method, a.weight, a.actor, a.reason, a.created_at "
        "FROM measurement_association_actions a JOIN targets t ON t.id=a.target_id"
    )
    op.execute("DROP VIEW IF EXISTS current_measurement_assignments")
    op.execute(
        "CREATE VIEW current_measurement_assignments AS "
        "SELECT a.id, a.measurement_id, a.target_id, t.sdbid, a.role, a.method, "
        "a.weight, a.note, a.created_at "
        "FROM measurement_target_associations a JOIN targets t ON t.id=a.target_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS current_measurement_assignments")
    op.execute("DROP VIEW IF EXISTS measurement_assignment_history")
    op.execute("DROP VIEW IF EXISTS current_target_lifecycle")
    tables = set(inspect(op.get_bind()).get_table_names())
    if "measurement_association_actions" in tables:
        op.drop_table("measurement_association_actions")
    if "target_lifecycle_actions" in tables:
        op.drop_table("target_lifecycle_actions")
