"""Store proper motion fields in SIMBAD metadata snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0032_simbad_metadata_proper_motion"
down_revision = "0031_match_candidate_astrometry_metadata"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("simbad_metadata")}
    additions = [
        ("pm_ra_cosdec_masyr", sa.Column("pm_ra_cosdec_masyr", sa.Float(), nullable=True)),
        ("pm_dec_masyr", sa.Column("pm_dec_masyr", sa.Float(), nullable=True)),
        ("proper_motion_bibcode", sa.Column("proper_motion_bibcode", sa.String(length=30), nullable=True)),
    ]
    for name, column in additions:
        if name not in existing:
            op.add_column("simbad_metadata", column)


def downgrade():
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("simbad_metadata")}
    for name in (
        "proper_motion_bibcode",
        "pm_dec_masyr",
        "pm_ra_cosdec_masyr",
    ):
        if name in existing:
            op.drop_column("simbad_metadata", name)
