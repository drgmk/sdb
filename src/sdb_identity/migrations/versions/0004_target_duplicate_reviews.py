"""persist possible duplicate target imports for review

Revision ID: 0004_target_duplicate_reviews
Revises: 0003_unified_exports
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_target_duplicate_reviews"
down_revision = "0003_unified_exports"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "target_duplicate_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("possible_duplicate_target_id", sa.Integer(), nullable=False),
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("separation_arcsec", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="review", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_id != possible_duplicate_target_id",
            name="ck_target_duplicate_reviews_distinct_targets",
        ),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["possible_duplicate_target_id"], ["targets.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_id", "possible_duplicate_target_id"),
    )
    with op.batch_alter_table("target_duplicate_reviews") as batch_op:
        for column in (
            "target_id", "possible_duplicate_target_id", "submission_id", "status",
        ):
            batch_op.create_index(
                batch_op.f(f"ix_target_duplicate_reviews_{column}"),
                [column], unique=False,
            )


def downgrade():
    raise NotImplementedError("development schema migrations are forward-only")
