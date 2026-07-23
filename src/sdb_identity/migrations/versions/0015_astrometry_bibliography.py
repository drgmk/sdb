"""Bibliography for astrometry used by identity and propagation."""

import sqlalchemy as sa
from alembic import op


revision = "0015_astrometry_bibliography"
down_revision = "0014_export_dirty_targets"
branch_labels = None
depends_on = None


def upgrade():
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("astrometric_solutions")
    }
    for name in (
        "position_bibcode",
        "proper_motion_bibcode",
        "parallax_bibcode",
        "radial_velocity_bibcode",
    ):
        if name not in existing:
            op.add_column("astrometric_solutions", sa.Column(name, sa.String(19)))
    op.execute("DROP VIEW IF EXISTS target_summary")
    op.execute(
        "CREATE VIEW target_summary AS "
        "SELECT t.id, t.sdbid, t.ra2000_deg, t.dec2000_deg, "
        "a.source AS astrometry_source, a.proper_motion_available, "
        "a.position_bibcode, a.proper_motion_bibcode, "
        "a.parallax_bibcode, a.radial_velocity_bibcode "
        "FROM targets t LEFT JOIN astrometric_solutions a "
        "ON a.id=t.canonical_astrometry_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS target_summary")
    op.execute(
        "CREATE VIEW target_summary AS "
        "SELECT t.id, t.sdbid, t.ra2000_deg, t.dec2000_deg, "
        "a.source AS astrometry_source, a.proper_motion_available "
        "FROM targets t LEFT JOIN astrometric_solutions a "
        "ON a.id=t.canonical_astrometry_id"
    )
    for name in (
        "radial_velocity_bibcode",
        "parallax_bibcode",
        "proper_motion_bibcode",
        "position_bibcode",
    ):
        op.drop_column("astrometric_solutions", name)
