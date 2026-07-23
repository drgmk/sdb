"""Enrich SIMBAD hierarchy relationships with related-object metadata."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0030_simbad_relationship_metadata"
down_revision = "0029_hierarchy_graph"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("simbad_relationships")}
    if "related_object_type" not in existing:
        op.add_column("simbad_relationships", sa.Column("related_object_type", sa.String(length=40), nullable=True))
    if "related_object_types_json" not in existing:
        op.add_column("simbad_relationships", sa.Column("related_object_types_json", sa.Text(), nullable=True))
    if "related_spectral_type" not in existing:
        op.add_column("simbad_relationships", sa.Column("related_spectral_type", sa.String(length=100), nullable=True))
    if "related_spectral_type_bibcode" not in existing:
        op.add_column("simbad_relationships", sa.Column("related_spectral_type_bibcode", sa.String(length=30), nullable=True))


def downgrade():
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("simbad_relationships")}
    if "related_spectral_type_bibcode" in existing:
        op.drop_column("simbad_relationships", "related_spectral_type_bibcode")
    if "related_spectral_type" in existing:
        op.drop_column("simbad_relationships", "related_spectral_type")
    if "related_object_types_json" in existing:
        op.drop_column("simbad_relationships", "related_object_types_json")
    if "related_object_type" in existing:
        op.drop_column("simbad_relationships", "related_object_type")
