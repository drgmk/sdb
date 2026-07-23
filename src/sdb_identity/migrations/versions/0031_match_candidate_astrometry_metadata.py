"""Store native astrometry metadata for identity match candidates."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0031_match_candidate_astrometry_metadata"
down_revision = "0030_simbad_relationship_metadata"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("match_candidates")}
    additions = [
        ("pm_ra_cosdec_masyr", sa.Column("pm_ra_cosdec_masyr", sa.Float(), nullable=True)),
        ("pm_dec_masyr", sa.Column("pm_dec_masyr", sa.Float(), nullable=True)),
        (
            "proper_motion_available",
            sa.Column("proper_motion_available", sa.Boolean(), nullable=False, server_default=sa.false()),
        ),
        ("parallax_mas", sa.Column("parallax_mas", sa.Float(), nullable=True)),
        ("radial_velocity_kms", sa.Column("radial_velocity_kms", sa.Float(), nullable=True)),
        ("position_bibcode", sa.Column("position_bibcode", sa.String(length=30), nullable=True)),
        ("proper_motion_bibcode", sa.Column("proper_motion_bibcode", sa.String(length=30), nullable=True)),
        ("parallax_bibcode", sa.Column("parallax_bibcode", sa.String(length=30), nullable=True)),
        ("radial_velocity_bibcode", sa.Column("radial_velocity_bibcode", sa.String(length=30), nullable=True)),
    ]
    for name, column in additions:
        if name not in existing:
            op.add_column("match_candidates", column)


def downgrade():
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("match_candidates")}
    for name in (
        "radial_velocity_bibcode",
        "parallax_bibcode",
        "proper_motion_bibcode",
        "position_bibcode",
        "radial_velocity_kms",
        "parallax_mas",
        "proper_motion_available",
        "pm_dec_masyr",
        "pm_ra_cosdec_masyr",
    ):
        if name in existing:
            op.drop_column("match_candidates", name)
