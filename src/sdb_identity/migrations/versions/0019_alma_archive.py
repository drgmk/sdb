"""Cached ALMA ObsCore metadata."""

from alembic import op

from sdb_identity.models import Base


revision = "0019_alma_archive"
down_revision = "0018_catalog_bulk_requests"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["alma_sync_runs"],
            Base.metadata.tables["alma_observations"],
        ],
    )
    op.execute(
        "CREATE VIEW alma_archive_status AS "
        "SELECT r.*, (SELECT COUNT(*) FROM alma_observations o WHERE o.active=1) "
        "AS active_observation_count FROM alma_sync_runs r"
    )
    op.execute(
        "CREATE VIEW current_alma_projects AS "
        "SELECT proposal_id, COUNT(*) AS observation_count, MIN(t_min_mjd) AS first_mjd, "
        "MAX(t_max_mjd) AS last_mjd, GROUP_CONCAT(DISTINCT band_list) AS band_lists "
        "FROM alma_observations WHERE active=1 GROUP BY proposal_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS current_alma_projects")
    op.execute("DROP VIEW IF EXISTS alma_archive_status")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["alma_observations"],
            Base.metadata.tables["alma_sync_runs"],
        ],
    )
