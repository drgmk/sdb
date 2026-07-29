"""Restore provider-native TDSC component scope."""

from alembic import op


revision = "0047_restore_tdsc_component_scope"
down_revision = "0046_restore_provider_photometry_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
        UPDATE normalized_measurements
        SET ownership_scope = 'component',
            blend_state = 'clear',
            blend_reason = NULL
        WHERE provider = 'tdsc'
        """
    )


def downgrade():
    # Encounter-order overwrites cannot be reconstructed.
    pass
