"""Drop legacy hierarchy edge tables now that structural_edges is the single source.

`hierarchy_graph_edges` + `hierarchy_graph_overrides` + `target_relationships` are
replaced by `structural_edges` + `structural_edge_actions` (created in 0038). This
migration:
  * repoints `hierarchy_match_actions.relationship_id` at `structural_edges`
    (rebuilds the table because SQLite cannot alter a foreign key in place),
  * drops the three legacy tables and their views, and
  * recreates the `hierarchy_relationship_summary` and `hierarchy_graph_effective`
    views over `structural_edges`/`structural_edge_actions`.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0039_drop_hierarchy_edge_tables"
down_revision = "0038_structural_edges"
branch_labels = None
depends_on = None


_MATCH_ACTION_COLUMNS = (
    "id", "candidate_id", "action", "previous_status", "new_status",
    "actor", "reason", "system_id", "relationship_id", "created_at",
)

_HIERARCHY_MATCH_ACTION_HISTORY = (
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

_HIERARCHY_RELATIONSHIP_SUMMARY = (
    "CREATE VIEW hierarchy_relationship_summary AS "
    "SELECT e.id, e.relation_type AS relationship_type, e.source, e.status, e.confidence, "
    "e.component_label AS component, e.separation_arcsec, e.pa_deg, e.relation_epoch, "
    "p.sdbid AS parent_sdbid, c.sdbid AS child_sdbid, "
    "a.sdbid AS primary_sdbid, b.sdbid AS secondary_sdbid, "
    "s.name AS system_name, e.reason, e.actor, e.created_at "
    "FROM structural_edges e "
    "LEFT JOIN targets p ON p.id = CASE e.direction "
    "WHEN 'a_parent_b' THEN e.endpoint_a_target_id "
    "WHEN 'b_parent_a' THEN e.endpoint_b_target_id END "
    "LEFT JOIN targets c ON c.id = CASE e.direction "
    "WHEN 'a_parent_b' THEN e.endpoint_b_target_id "
    "WHEN 'b_parent_a' THEN e.endpoint_a_target_id END "
    "LEFT JOIN targets a ON a.id = CASE e.direction "
    "WHEN 'pair' THEN e.endpoint_a_target_id END "
    "LEFT JOIN targets b ON b.id = CASE e.direction "
    "WHEN 'pair' THEN e.endpoint_b_target_id END "
    "LEFT JOIN target_systems s ON s.id = e.system_id "
    "WHERE e.status = 'accepted'"
)

_HIERARCHY_GRAPH_EFFECTIVE = (
    "CREATE VIEW hierarchy_graph_effective AS "
    "SELECT e.id AS edge_id, e.source_id, e.record_id, e.source AS provider, e.native_id, "
    "e.source_component, e.reference_label, e.component_label, "
    "COALESCE(o.new_relation_type, e.relation_type) AS relation_type, "
    "COALESCE(o.new_structural_role, e.structural_role) AS structural_role, "
    "COALESCE(o.new_status, e.status) AS status, "
    "e.geometry_status, e.start_ra_deg, e.start_dec_deg, e.end_ra_deg, e.end_dec_deg, "
    "e.separation_arcsec, e.pa_deg, e.relation_epoch, e.note, "
    "o.id AS override_id, o.actor AS override_actor, o.reason AS override_reason, "
    "o.created_at AS override_created_at "
    "FROM structural_edges e "
    "LEFT JOIN structural_edge_actions o ON o.id = ("
    "SELECT oo.id FROM structural_edge_actions oo "
    "WHERE oo.edge_id = e.id OR ("
    "oo.source = e.source AND oo.native_id = e.native_id "
    "AND COALESCE(oo.reference_label, '') = COALESCE(e.reference_label, '') "
    "AND COALESCE(oo.component_label, '') = COALESCE(e.component_label, '')) "
    "ORDER BY oo.created_at DESC, oo.id DESC LIMIT 1) "
    "WHERE e.status IN ('derived', 'stale', 'rejected')"
)


def _rebuild_match_actions(relationship_fk: str) -> None:
    op.execute("DROP VIEW IF EXISTS hierarchy_match_action_history")
    # Rename the old table aside; its indexes follow it and keep their global
    # names, so the new indexes can only be created after the old table is gone.
    op.rename_table("hierarchy_match_actions", "_old_hierarchy_match_actions")
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
        sa.Column("relationship_id", sa.Integer(), sa.ForeignKey(relationship_fk)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "INSERT INTO hierarchy_match_actions ({cols}) SELECT {cols} FROM _old_hierarchy_match_actions".format(
            cols=", ".join(_MATCH_ACTION_COLUMNS)
        )
    )
    op.drop_table("_old_hierarchy_match_actions")
    op.create_index("ix_hierarchy_match_actions_candidate_id", "hierarchy_match_actions", ["candidate_id"])
    op.create_index("ix_hierarchy_match_actions_action", "hierarchy_match_actions", ["action"])
    op.execute(_HIERARCHY_MATCH_ACTION_HISTORY)


def upgrade():
    tables = set(inspect(op.get_bind()).get_table_names())

    # Repoint hierarchy_match_actions.relationship_id -> structural_edges.
    _rebuild_match_actions("structural_edges.id")

    # Drop legacy views + tables.
    op.execute("DROP VIEW IF EXISTS hierarchy_graph_effective")
    op.execute("DROP VIEW IF EXISTS hierarchy_relationship_summary")
    if "hierarchy_graph_overrides" in tables:
        op.drop_table("hierarchy_graph_overrides")
    if "hierarchy_graph_edges" in tables:
        op.drop_table("hierarchy_graph_edges")
    if "target_relationships" in tables:
        op.drop_table("target_relationships")

    # Recreate the views over structural_edges/structural_edge_actions.
    op.execute(_HIERARCHY_RELATIONSHIP_SUMMARY)
    op.execute(_HIERARCHY_GRAPH_EFFECTIVE)


def downgrade():
    raise NotImplementedError(
        "0039 consolidates hierarchy edges into structural_edges and cannot be reversed"
    )
