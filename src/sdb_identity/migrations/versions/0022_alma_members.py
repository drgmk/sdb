"""One cached row per ALMA member OUS."""

from alembic import op

from sdb_identity.models import Base


revision = "0022_alma_members"
down_revision = "0021_alma_lookup_indexes"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(), tables=[Base.metadata.tables["alma_members"]]
    )
    op.execute("DROP VIEW IF EXISTS current_alma_projects")
    op.execute(
        "CREATE VIEW current_alma_projects AS "
        "SELECT proposal_id, COUNT(*) AS member_count, MIN(t_min_mjd) AS first_mjd, "
        "MAX(t_max_mjd) AS last_mjd, GROUP_CONCAT(DISTINCT band_list) AS band_lists "
        "FROM alma_members WHERE active=1 GROUP BY proposal_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS current_alma_projects")
    op.execute(
        "CREATE VIEW current_alma_projects AS "
        "SELECT proposal_id, COUNT(*) AS observation_count, MIN(t_min_mjd) AS first_mjd, "
        "MAX(t_max_mjd) AS last_mjd, GROUP_CONCAT(DISTINCT band_list) AS band_lists "
        "FROM alma_observations WHERE active=1 GROUP BY proposal_id"
    )
    Base.metadata.drop_all(
        bind=op.get_bind(), tables=[Base.metadata.tables["alma_members"]]
    )
