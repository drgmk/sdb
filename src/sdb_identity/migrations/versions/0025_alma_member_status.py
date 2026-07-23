"""Report member-level ALMA cache counts."""

from alembic import op


revision = "0025_alma_member_status"
down_revision = "0024_alma_member_positions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP VIEW IF EXISTS alma_archive_status")
    op.execute(
        "CREATE VIEW alma_archive_status AS "
        "SELECT r.*, (SELECT COUNT(*) FROM alma_members m WHERE m.active=1) "
        "AS active_member_count, "
        "(SELECT COUNT(*) FROM alma_member_positions p) AS pointing_count "
        "FROM alma_sync_runs r"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS alma_archive_status")
    op.execute(
        "CREATE VIEW alma_archive_status AS "
        "SELECT r.*, (SELECT COUNT(*) FROM alma_observations o WHERE o.active=1) "
        "AS active_observation_count FROM alma_sync_runs r"
    )
