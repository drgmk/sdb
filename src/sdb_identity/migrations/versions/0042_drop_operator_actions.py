"""Drop the redundant untyped identity operator audit table."""

from alembic import op


revision = "0042_drop_operator_actions"
down_revision = "0041_photometry_semantics"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("operator_actions")


def downgrade():
    raise NotImplementedError(
        "0042 removes a duplicate untyped audit table and cannot be reversed"
    )
