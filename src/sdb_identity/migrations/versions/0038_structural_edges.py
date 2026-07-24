"""Add unified structural-edge tables.

`structural_edges` (current state) + `structural_edge_actions` (append-only history)
will replace `hierarchy_graph_edges` + `hierarchy_graph_overrides` + `target_relationships`.
This migration only creates the new tables; the old ones are dropped in a later
migration once writers/readers have been repointed.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0038_structural_edges"
down_revision = "0037_drop_photometry_association_decisions"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(inspect(op.get_bind()).get_table_names())
    if "structural_edges" not in tables:
        op.create_table(
            "structural_edges",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source", sa.String(40), nullable=False, index=True),
            sa.Column("source_id", sa.Integer(), sa.ForeignKey("hierarchy_sources.id"), index=True),
            sa.Column("record_id", sa.Integer(), sa.ForeignKey("hierarchy_records.id"), index=True),
            sa.Column("native_id", sa.String(200), index=True),
            sa.Column("system_id", sa.Integer(), sa.ForeignKey("target_systems.id"), index=True),
            sa.Column("endpoint_a_target_id", sa.Integer(), sa.ForeignKey("targets.id"), index=True),
            sa.Column("endpoint_b_target_id", sa.Integer(), sa.ForeignKey("targets.id"), index=True),
            sa.Column("reference_label", sa.String(80), index=True),
            sa.Column("component_label", sa.String(80), index=True),
            sa.Column("source_component", sa.String(80), index=True),
            sa.Column("direction", sa.String(20), nullable=False, server_default="pair"),
            sa.Column("relation_type", sa.String(40), nullable=False, index=True),
            sa.Column("structural_role", sa.String(40), nullable=False, server_default="non_structural", index=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="derived", index=True),
            sa.Column("confidence", sa.String(30), nullable=False, server_default="unknown"),
            sa.Column("geometry_status", sa.String(30), index=True),
            sa.Column("start_ra_deg", sa.Float()),
            sa.Column("start_dec_deg", sa.Float()),
            sa.Column("end_ra_deg", sa.Float()),
            sa.Column("end_dec_deg", sa.Float()),
            sa.Column("separation_arcsec", sa.Float()),
            sa.Column("pa_deg", sa.Float()),
            sa.Column("relation_epoch", sa.Float()),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("actor", sa.String(100)),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_structural_edges_source_native", "structural_edges", ["source", "native_id"])
    if "structural_edge_actions" not in tables:
        op.create_table(
            "structural_edge_actions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("edge_id", sa.Integer(), sa.ForeignKey("structural_edges.id"), index=True),
            sa.Column("source", sa.String(40), nullable=False, index=True),
            sa.Column("native_id", sa.String(200), index=True),
            sa.Column("reference_label", sa.String(80), index=True),
            sa.Column("component_label", sa.String(80), index=True),
            sa.Column("action", sa.String(40), nullable=False, index=True),
            sa.Column("previous_status", sa.String(30)),
            sa.Column("new_status", sa.String(30)),
            sa.Column("previous_relation_type", sa.String(40)),
            sa.Column("new_relation_type", sa.String(40)),
            sa.Column("previous_structural_role", sa.String(40)),
            sa.Column("new_structural_role", sa.String(40)),
            sa.Column("actor", sa.String(100), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_structural_edge_actions_source_native", "structural_edge_actions", ["source", "native_id"])


def downgrade():
    tables = set(inspect(op.get_bind()).get_table_names())
    if "structural_edge_actions" in tables:
        op.drop_table("structural_edge_actions")
    if "structural_edges" in tables:
        op.drop_table("structural_edges")
