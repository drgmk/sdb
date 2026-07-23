"""Spatial bounds for member-level ALMA lookup."""

import sqlalchemy as sa
from alembic import op


revision = "0023_alma_member_bounds"
down_revision = "0022_alma_members"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {value["name"] for value in sa.inspect(bind).get_columns("alma_members")}
    for name in ("center_ra_deg", "center_dec_deg", "bounding_radius_deg"):
        if name not in columns:
            op.add_column("alma_members", sa.Column(name, sa.Float()))
    indexes = {
        value["name"] for value in sa.inspect(bind).get_indexes("alma_members")
    }
    if "ix_alma_members_center_dec_deg" not in indexes:
        op.create_index(
            "ix_alma_members_center_dec_deg", "alma_members", ["center_dec_deg"]
        )


def downgrade():
    op.drop_index("ix_alma_members_center_dec_deg", table_name="alma_members")
    op.drop_column("alma_members", "bounding_radius_deg")
    op.drop_column("alma_members", "center_dec_deg")
    op.drop_column("alma_members", "center_ra_deg")
