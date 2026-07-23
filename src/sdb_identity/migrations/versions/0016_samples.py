"""Samples and append-only target membership."""

from alembic import op

from sdb_identity.models import Base


revision = "0016_samples"
down_revision = "0015_astrometry_bibliography"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["samples"],
            Base.metadata.tables["sample_membership_actions"],
        ],
    )
    op.execute(
        "CREATE VIEW current_sample_memberships AS "
        "SELECT a.id AS action_id, a.sample_id, s.name AS sample_name, "
        "a.target_id, t.sdbid, a.actor, a.reason, a.created_at "
        "FROM sample_membership_actions a "
        "JOIN samples s ON s.id=a.sample_id "
        "JOIN targets t ON t.id=a.target_id "
        "WHERE a.action='add' AND a.id=(SELECT MAX(x.id) "
        "FROM sample_membership_actions x "
        "WHERE x.sample_id=a.sample_id AND x.target_id=a.target_id)"
    )
    op.execute(
        "CREATE VIEW sample_summary AS "
        "SELECT s.id, s.name, s.sample_date, s.note, s.created_at, s.updated_at, "
        "COUNT(m.target_id) AS member_count FROM samples s "
        "LEFT JOIN current_sample_memberships m ON m.sample_id=s.id "
        "GROUP BY s.id, s.name, s.sample_date, s.note, s.created_at, s.updated_at"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS sample_summary")
    op.execute("DROP VIEW IF EXISTS current_sample_memberships")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["sample_membership_actions"],
            Base.metadata.tables["samples"],
        ],
    )
