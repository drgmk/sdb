"""Unify stored and predicted photometry ownership/blending vocabulary."""

from alembic import op
from sqlalchemy import inspect


revision = "0041_photometry_semantics"
down_revision = "0040_drop_source_dirty_tables"
branch_labels = None
depends_on = None


def upgrade():
    # Direct SQLite column renames avoid Alembic batch mode's circular column
    # ordering when two adjacent columns are renamed in the same table rebuild.
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("normalized_measurements")
    }
    op.execute("DROP VIEW IF EXISTS blend_review")
    if "ownership_scope" not in columns:
        op.execute(
            "ALTER TABLE normalized_measurements "
            "RENAME COLUMN association_scope TO ownership_scope"
        )
    else:
        op.execute(
            "UPDATE normalized_measurements SET ownership_scope = association_scope"
        )
        op.execute(
            "ALTER TABLE normalized_measurements DROP COLUMN association_scope"
        )
    if "blend_state" not in columns:
        op.execute(
            "ALTER TABLE normalized_measurements "
            "RENAME COLUMN blend_status TO blend_state"
        )
    else:
        op.execute("UPDATE normalized_measurements SET blend_state = blend_status")
        op.execute("ALTER TABLE normalized_measurements DROP COLUMN blend_status")
    if "blend_reason" not in columns:
        op.execute(
            "ALTER TABLE normalized_measurements ADD COLUMN blend_reason VARCHAR(80)"
        )

    op.execute(
        "UPDATE normalized_measurements SET blend_reason = blend_state "
        "WHERE blend_state NOT IN ('clear', 'blended', 'ambiguous', 'unknown')"
    )
    op.execute(
        "UPDATE normalized_measurements SET blend_state = CASE "
        "WHEN blend_state = 'clear' THEN 'clear' "
        "WHEN blend_state IN ('hierarchy_ambiguous', 'ambiguous') THEN 'ambiguous' "
        "WHEN blend_state IN ('unknown_resolution', 'no_nearby_component_estimate', 'unknown') "
        "THEN 'unknown' ELSE 'blended' END"
    )
    op.execute(
        "UPDATE normalized_measurements SET ownership_scope = 'shared' "
        "WHERE ownership_scope = 'blended'"
    )
    op.execute(
        "CREATE VIEW blend_review AS "
        "SELECT m.*, t.sdbid, r.target_id AS encounter_target_id "
        "FROM normalized_measurements m "
        "JOIN raw_catalog_rows raw ON raw.detection_id=m.detection_id "
        "JOIN catalog_runs r ON r.id=raw.run_id "
        "JOIN targets t ON t.id=r.target_id "
        "WHERE r.is_current=1 AND r.status='match' AND raw.accepted=1 "
        "AND (m.blend_state!='clear' OR m.ownership_scope!='component')"
    )


def downgrade():
    raise NotImplementedError(
        "0041 replaces overloaded photometry vocabulary and cannot be reversed"
    )
