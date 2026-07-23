"""Versioned PSC/FSC detection families and per-band selections."""

from alembic import op

from sdb_identity.models import Base


revision = "0013_iras_detection_families"
down_revision = "0012_resolution_blends_iras"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind(), tables=[
        Base.metadata.tables["iras_detection_families"],
        Base.metadata.tables["iras_band_selections"],
    ])
    op.execute(
        "CREATE VIEW current_iras_band_selections AS "
        "SELECT f.target_id, f.status, f.normalized_separation, s.* "
        "FROM iras_detection_families f "
        "JOIN iras_band_selections s ON s.family_id=f.id "
        "WHERE f.is_current=1"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS current_iras_band_selections")
    Base.metadata.drop_all(bind=op.get_bind(), tables=[
        Base.metadata.tables["iras_band_selections"],
        Base.metadata.tables["iras_detection_families"],
    ])
