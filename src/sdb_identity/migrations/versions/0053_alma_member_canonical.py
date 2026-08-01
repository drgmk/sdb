"""Make member OUS rows and normalized positions the only ALMA cache state."""

from alembic import op
import sqlalchemy as sa


revision = "0053_alma_member_canonical"
down_revision = "0052_identity_candidate_decisions"
branch_labels = None
depends_on = None


_COMPOSITE_UNIQUE = "uq_alma_members_proposal_member_ous"
_NAMING_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    op.execute("DROP VIEW IF EXISTS current_alma_projects")
    op.execute("DROP VIEW IF EXISTS alma_archive_status")

    if "alma_members" in tables:
        op.execute(
            "UPDATE alma_members SET member_ous_uid="
            "substr(member_ous_uid, length(proposal_id) + 2) "
            "WHERE member_ous_uid LIKE proposal_id || '|%'"
        )
        columns = {
            value["name"] for value in inspector.get_columns("alma_members")
        }
        unique_constraints = inspector.get_unique_constraints("alma_members")
        old_unique = next(
            (
                value
                for value in unique_constraints
                if value["column_names"] == ["member_ous_uid"]
            ),
            None,
        )
        has_composite = any(
            value["column_names"] == ["proposal_id", "member_ous_uid"]
            for value in unique_constraints
        )
        if old_unique is not None or "positions_json" in columns or not has_composite:
            with op.batch_alter_table(
                "alma_members",
                naming_convention=_NAMING_CONVENTION,
                recreate="always",
            ) as batch:
                if old_unique is not None:
                    batch.drop_constraint(
                        old_unique["name"] or "uq_alma_members_member_ous_uid",
                        type_="unique",
                    )
                if "positions_json" in columns:
                    batch.drop_column("positions_json")
                if not has_composite:
                    batch.create_unique_constraint(
                        _COMPOSITE_UNIQUE,
                        ["proposal_id", "member_ous_uid"],
                    )

    if "alma_observations" in tables:
        op.drop_table("alma_observations")

    op.execute(
        "CREATE VIEW alma_archive_status AS "
        "SELECT r.*, (SELECT COUNT(*) FROM alma_members m WHERE m.active=1) "
        "AS active_member_count, "
        "(SELECT COUNT(*) FROM alma_member_positions p) AS pointing_count "
        "FROM alma_sync_runs r"
    )
    op.execute(
        "CREATE VIEW current_alma_projects AS "
        "SELECT proposal_id, COUNT(*) AS member_count, MIN(t_min_mjd) AS first_mjd, "
        "MAX(t_max_mjd) AS last_mjd, GROUP_CONCAT(DISTINCT band_list) AS band_lists "
        "FROM alma_members WHERE active=1 GROUP BY proposal_id"
    )


def downgrade():
    raise RuntimeError("development schema migrations are forward-only")
