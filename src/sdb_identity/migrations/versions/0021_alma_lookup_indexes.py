"""Indexes for local ALMA sky lookup."""

import sqlalchemy as sa
from alembic import op


revision = "0021_alma_lookup_indexes"
down_revision = "0020_alma_sync_chunks"
branch_labels = None
depends_on = None


def upgrade():
    indexes = {
        value["name"]
        for value in sa.inspect(op.get_bind()).get_indexes("alma_observations")
    }
    if "ix_alma_observations_active_dec_ra" not in indexes:
        op.create_index(
            "ix_alma_observations_active_dec_ra",
            "alma_observations",
            ["active", "dec_deg", "ra_deg"],
        )
    if "ix_alma_observations_fov_deg" not in indexes:
        op.create_index(
            "ix_alma_observations_fov_deg",
            "alma_observations",
            ["fov_deg"],
        )


def downgrade():
    op.drop_index("ix_alma_observations_fov_deg", table_name="alma_observations")
    op.drop_index(
        "ix_alma_observations_active_dec_ra", table_name="alma_observations"
    )
