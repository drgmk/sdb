"""Add audited target-to-catalog-detection association decisions."""

from alembic import op

from sdb_identity.models import Base


revision = "0045_catalog_target_associations"
down_revision = "0044_detection_normalization_state"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["catalog_target_association_actions"]
        ],
    )


def downgrade():
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["catalog_target_association_actions"]
        ],
    )
