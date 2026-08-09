"""make export runs selection-based rather than sample-owned

Revision ID: 0003_unified_exports
Revises: 0002_iras_source_families
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_unified_exports"
down_revision = "0002_iras_source_families"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP VIEW sample_export_summary")
    op.drop_table("sample_export_items")
    op.drop_table("sample_export_runs")

    op.create_table(
        "export_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("selection_kind", sa.String(length=20), nullable=False),
        sa.Column("selection_value", sa.String(length=200), nullable=True),
        sa.Column("output_dir", sa.Text(), nullable=False),
        sa.Column("database_revision", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("export_runs") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_export_runs_selection_kind"),
            ["selection_kind"], unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_export_runs_selection_value"),
            ["selection_value"], unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_export_runs_status"), ["status"], unique=False,
        )

    op.create_table(
        "export_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("output_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["export_runs.id"]),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "target_id"),
    )
    with op.batch_alter_table("export_items") as batch_op:
        for column in ("run_id", "target_id", "package_id", "status"):
            batch_op.create_index(
                batch_op.f(f"ix_export_items_{column}"),
                [column], unique=False,
            )

    op.execute("""
        CREATE VIEW export_summary AS
        SELECT r.id AS run_id, r.selection_kind, r.selection_value,
               r.output_dir, r.database_revision, r.status, r.manifest_path,
               r.started_at, r.completed_at,
               COUNT(i.id) AS target_count,
               SUM(CASE WHEN i.status='exported' THEN 1 ELSE 0 END)
                   AS exported_count,
               SUM(CASE WHEN i.status='skipped' THEN 1 ELSE 0 END)
                   AS skipped_count,
               SUM(CASE WHEN i.status='failed' THEN 1 ELSE 0 END)
                   AS failed_count
        FROM export_runs r
        LEFT JOIN export_items i ON i.run_id=r.id
        GROUP BY r.id
    """)


def downgrade():
    raise NotImplementedError("development schema migrations are forward-only")
