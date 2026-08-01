"""Cached ALMA ObsCore metadata."""

from alembic import op
import sqlalchemy as sa

from sdb_identity.models import Base


revision = "0019_alma_archive"
down_revision = "0018_catalog_bulk_requests"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["alma_sync_runs"]],
    )
    # This legacy product-row table exists only until the member-level cache
    # migration. Define it here rather than retaining a runtime ORM model.
    op.create_table(
        "alma_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.String(300), unique=True, nullable=False),
        sa.Column("proposal_id", sa.String(100), nullable=False),
        sa.Column("target_name", sa.String(300)),
        sa.Column("ra_deg", sa.Float(), nullable=False),
        sa.Column("dec_deg", sa.Float(), nullable=False),
        sa.Column("fov_deg", sa.Float()),
        sa.Column("region", sa.Text()),
        sa.Column("t_min_mjd", sa.Float()),
        sa.Column("t_max_mjd", sa.Float()),
        sa.Column("release_date", sa.String(40)),
        sa.Column("data_rights", sa.String(40)),
        sa.Column("band_list", sa.String(100)),
        sa.Column("last_modified", sa.String(40)),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "first_seen_run_id",
            sa.Integer(),
            sa.ForeignKey("alma_sync_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_run_id",
            sa.Integer(),
            sa.ForeignKey("alma_sync_runs.id"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False, default=True),
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
    op.drop_table("alma_observations")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["alma_sync_runs"]],
    )
