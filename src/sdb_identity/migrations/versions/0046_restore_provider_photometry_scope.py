"""Restore provider-native UBV component and blend semantics."""

from alembic import op


revision = "0046_restore_provider_photometry_scope"
down_revision = "0045_catalog_target_associations"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.exec_driver_sql(
        """
        UPDATE normalized_measurements
        SET ownership_scope = CASE
                WHEN upper(source_id) LIKE '%|M_LID=D' THEN 'system'
                WHEN upper(source_id) LIKE '%|M_LID=S' THEN 'ambiguous'
                ELSE 'component'
            END,
            blend_state = CASE
                WHEN upper(source_id) LIKE '%|M_LID=D' THEN 'blended'
                WHEN upper(source_id) LIKE '%|M_LID=S' THEN 'unknown'
                ELSE 'clear'
            END,
            blend_reason = CASE
                WHEN upper(source_id) LIKE '%|M_LID=D'
                    THEN 'catalog_multiple_in_aperture'
                WHEN upper(source_id) LIKE '%|M_LID=S'
                    THEN 'catalog_supplementary_identifier'
                ELSE NULL
            END
        WHERE provider = 'ubvmeans'
        """
    )


def downgrade():
    # The pre-migration values may have been overwritten by import order and
    # cannot be reconstructed. Keep the valid provider-native representation.
    pass
