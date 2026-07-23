"""Add composite indexes for per-system hierarchy lookups."""

from alembic import op


revision = "0034_hierarchy_lookup_indexes"
down_revision = "0033_photometry_association_decisions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_hierarchy_records_source_provider_native",
        "hierarchy_records",
        ["source_id", "provider", "native_id"],
    )
    op.create_index(
        "ix_hierarchy_graph_edges_source_provider_native",
        "hierarchy_graph_edges",
        ["source_id", "provider", "native_id"],
    )
    op.create_index(
        "ix_hierarchy_graph_overrides_source_provider_native",
        "hierarchy_graph_overrides",
        ["source_id", "provider", "native_id"],
    )


def downgrade():
    op.drop_index(
        "ix_hierarchy_graph_overrides_source_provider_native",
        table_name="hierarchy_graph_overrides",
    )
    op.drop_index(
        "ix_hierarchy_graph_edges_source_provider_native",
        table_name="hierarchy_graph_edges",
    )
    op.drop_index(
        "ix_hierarchy_records_source_provider_native",
        table_name="hierarchy_records",
    )
