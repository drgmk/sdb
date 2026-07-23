"""Deduplicated ALMA member pointing index."""

from alembic import op

from sdb_identity.models import Base


revision = "0024_alma_member_positions"
down_revision = "0023_alma_member_bounds"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["alma_member_positions"]],
    )
    op.create_index(
        "ix_alma_member_positions_dec_ra",
        "alma_member_positions",
        ["dec_deg", "ra_deg"],
    )
    op.create_index(
        "ix_alma_member_positions_fov_dec_ra",
        "alma_member_positions",
        ["fov_deg", "dec_deg", "ra_deg"],
    )


def downgrade():
    op.drop_index(
        "ix_alma_member_positions_fov_dec_ra",
        table_name="alma_member_positions",
    )
    op.drop_index(
        "ix_alma_member_positions_dec_ra", table_name="alma_member_positions"
    )
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["alma_member_positions"]],
    )
