"""Per-band resolution and conservative shared-source export state."""

import sqlalchemy as sa
from alembic import op


revision = "0012_resolution_blends_iras"
down_revision = "0011_catalog_attributes"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("normalized_measurements")}
    additions = (
        sa.Column("resolution_major_arcsec", sa.Float()),
        sa.Column("resolution_minor_arcsec", sa.Float()),
        sa.Column("resolution_kind", sa.String(40)),
        sa.Column("resolution_reference", sa.Text()),
        sa.Column("association_scope", sa.String(20), nullable=False, server_default="component"),
        sa.Column("blend_status", sa.String(30), nullable=False, server_default="clear"),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("normalized_measurements", column)
    indexes = {index["name"] for index in inspector.get_indexes("normalized_measurements")}
    if "ix_normalized_measurements_provider_source" not in indexes:
        op.create_index(
            "ix_normalized_measurements_provider_source",
            "normalized_measurements",
            ["provider", "source_id"],
        )
    op.execute(
        "CREATE VIEW blend_review AS "
        "SELECT m.*, t.sdbid FROM normalized_measurements m "
        "JOIN catalog_runs r ON r.id=m.run_id "
        "JOIN targets t ON t.id=m.target_id "
        "WHERE r.is_current=1 AND r.status='match' "
        "AND (m.blend_status!='clear' OR m.association_scope!='component')"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS blend_review")
    op.drop_index("ix_normalized_measurements_provider_source", table_name="normalized_measurements")
    for column in (
        "blend_status", "association_scope", "resolution_reference",
        "resolution_kind", "resolution_minor_arcsec", "resolution_major_arcsec",
    ):
        op.drop_column("normalized_measurements", column)
