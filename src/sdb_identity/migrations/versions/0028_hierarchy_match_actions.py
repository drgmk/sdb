"""Hierarchy match action history."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0028_hierarchy_match_actions"
down_revision = "0027_hierarchy_match_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hierarchy_match_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("hierarchy_match_candidates.id"), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("previous_status", sa.String(length=30), nullable=False),
        sa.Column("new_status", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("system_id", sa.Integer(), sa.ForeignKey("target_systems.id")),
        sa.Column("relationship_id", sa.Integer(), sa.ForeignKey("target_relationships.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hierarchy_match_actions_candidate_id", "hierarchy_match_actions", ["candidate_id"])
    op.create_index("ix_hierarchy_match_actions_action", "hierarchy_match_actions", ["action"])
    op.execute(
        "CREATE VIEW hierarchy_match_action_history AS "
        "SELECT a.id AS action_id, a.candidate_id, a.action, "
        "a.previous_status, a.new_status, a.actor, a.reason, "
        "a.system_id, a.relationship_id, a.created_at, "
        "c.provider, c.record_id, c.target_id, c.match_method, c.score, "
        "r.native_id, r.component, t.sdbid "
        "FROM hierarchy_match_actions a "
        "JOIN hierarchy_match_candidates c ON c.id = a.candidate_id "
        "JOIN hierarchy_records r ON r.id = c.record_id "
        "JOIN targets t ON t.id = c.target_id"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS hierarchy_match_action_history")
    op.drop_index("ix_hierarchy_match_actions_action", table_name="hierarchy_match_actions")
    op.drop_index("ix_hierarchy_match_actions_candidate_id", table_name="hierarchy_match_actions")
    op.drop_table("hierarchy_match_actions")
