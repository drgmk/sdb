"""Provider-derived hierarchy graph edges and overrides."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_hierarchy_graph"
down_revision = "0028_hierarchy_match_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hierarchy_graph_edges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("hierarchy_sources.id"), nullable=False),
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("hierarchy_records.id")),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("native_id", sa.String(length=200), nullable=False),
        sa.Column("source_component", sa.String(length=80)),
        sa.Column("reference_label", sa.String(length=80)),
        sa.Column("component_label", sa.String(length=80)),
        sa.Column("relation_type", sa.String(length=40), nullable=False),
        sa.Column("structural_role", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("geometry_status", sa.String(length=30), nullable=False),
        sa.Column("start_ra_deg", sa.Float()),
        sa.Column("start_dec_deg", sa.Float()),
        sa.Column("end_ra_deg", sa.Float()),
        sa.Column("end_dec_deg", sa.Float()),
        sa.Column("separation_arcsec", sa.Float()),
        sa.Column("pa_deg", sa.Float()),
        sa.Column("relation_epoch", sa.Float()),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hierarchy_graph_edges_source_id", "hierarchy_graph_edges", ["source_id"])
    op.create_index("ix_hierarchy_graph_edges_record_id", "hierarchy_graph_edges", ["record_id"])
    op.create_index("ix_hierarchy_graph_edges_provider", "hierarchy_graph_edges", ["provider"])
    op.create_index("ix_hierarchy_graph_edges_native_id", "hierarchy_graph_edges", ["native_id"])
    op.create_index("ix_hierarchy_graph_edges_source_component", "hierarchy_graph_edges", ["source_component"])
    op.create_index("ix_hierarchy_graph_edges_reference_label", "hierarchy_graph_edges", ["reference_label"])
    op.create_index("ix_hierarchy_graph_edges_component_label", "hierarchy_graph_edges", ["component_label"])
    op.create_index("ix_hierarchy_graph_edges_relation_type", "hierarchy_graph_edges", ["relation_type"])
    op.create_index("ix_hierarchy_graph_edges_structural_role", "hierarchy_graph_edges", ["structural_role"])
    op.create_index("ix_hierarchy_graph_edges_status", "hierarchy_graph_edges", ["status"])
    op.create_index("ix_hierarchy_graph_edges_geometry_status", "hierarchy_graph_edges", ["geometry_status"])

    op.create_table(
        "hierarchy_graph_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("edge_id", sa.Integer(), sa.ForeignKey("hierarchy_graph_edges.id")),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("hierarchy_sources.id")),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("native_id", sa.String(length=200), nullable=False),
        sa.Column("reference_label", sa.String(length=80)),
        sa.Column("component_label", sa.String(length=80)),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("previous_status", sa.String(length=30)),
        sa.Column("new_status", sa.String(length=30)),
        sa.Column("previous_relation_type", sa.String(length=40)),
        sa.Column("new_relation_type", sa.String(length=40)),
        sa.Column("previous_structural_role", sa.String(length=40)),
        sa.Column("new_structural_role", sa.String(length=40)),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hierarchy_graph_overrides_edge_id", "hierarchy_graph_overrides", ["edge_id"])
    op.create_index("ix_hierarchy_graph_overrides_source_id", "hierarchy_graph_overrides", ["source_id"])
    op.create_index("ix_hierarchy_graph_overrides_provider", "hierarchy_graph_overrides", ["provider"])
    op.create_index("ix_hierarchy_graph_overrides_native_id", "hierarchy_graph_overrides", ["native_id"])
    op.create_index("ix_hierarchy_graph_overrides_reference_label", "hierarchy_graph_overrides", ["reference_label"])
    op.create_index("ix_hierarchy_graph_overrides_component_label", "hierarchy_graph_overrides", ["component_label"])
    op.create_index("ix_hierarchy_graph_overrides_action", "hierarchy_graph_overrides", ["action"])

    op.execute(
        "CREATE VIEW hierarchy_graph_effective AS "
        "SELECT e.id AS edge_id, e.source_id, e.record_id, e.provider, e.native_id, "
        "e.source_component, e.reference_label, e.component_label, "
        "COALESCE(o.new_relation_type, e.relation_type) AS relation_type, "
        "COALESCE(o.new_structural_role, e.structural_role) AS structural_role, "
        "COALESCE(o.new_status, e.status) AS status, "
        "e.geometry_status, e.start_ra_deg, e.start_dec_deg, e.end_ra_deg, e.end_dec_deg, "
        "e.separation_arcsec, e.pa_deg, e.relation_epoch, e.note, "
        "o.id AS override_id, o.actor AS override_actor, o.reason AS override_reason, "
        "o.created_at AS override_created_at "
        "FROM hierarchy_graph_edges e "
        "LEFT JOIN hierarchy_graph_overrides o ON o.id = ("
        "SELECT oo.id FROM hierarchy_graph_overrides oo "
        "WHERE oo.edge_id = e.id OR ("
        "oo.source_id = e.source_id AND oo.provider = e.provider AND oo.native_id = e.native_id "
        "AND COALESCE(oo.reference_label, '') = COALESCE(e.reference_label, '') "
        "AND COALESCE(oo.component_label, '') = COALESCE(e.component_label, '')) "
        "ORDER BY oo.created_at DESC, oo.id DESC LIMIT 1)"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS hierarchy_graph_effective")
    op.drop_index("ix_hierarchy_graph_overrides_action", table_name="hierarchy_graph_overrides")
    op.drop_index("ix_hierarchy_graph_overrides_component_label", table_name="hierarchy_graph_overrides")
    op.drop_index("ix_hierarchy_graph_overrides_reference_label", table_name="hierarchy_graph_overrides")
    op.drop_index("ix_hierarchy_graph_overrides_native_id", table_name="hierarchy_graph_overrides")
    op.drop_index("ix_hierarchy_graph_overrides_provider", table_name="hierarchy_graph_overrides")
    op.drop_index("ix_hierarchy_graph_overrides_source_id", table_name="hierarchy_graph_overrides")
    op.drop_index("ix_hierarchy_graph_overrides_edge_id", table_name="hierarchy_graph_overrides")
    op.drop_table("hierarchy_graph_overrides")
    op.drop_index("ix_hierarchy_graph_edges_geometry_status", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_status", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_structural_role", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_relation_type", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_component_label", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_reference_label", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_source_component", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_native_id", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_provider", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_record_id", table_name="hierarchy_graph_edges")
    op.drop_index("ix_hierarchy_graph_edges_source_id", table_name="hierarchy_graph_edges")
    op.drop_table("hierarchy_graph_edges")
