"""Keep provider association IDs out of authoritative target aliases."""

from alembic import op

revision = "0010_catalog_identifier_policy"
down_revision = "0009_catalog_match_overrides"
branch_labels = None
depends_on = None


CATALOG_SOURCES = ("2mass", "allwise", "gaspar13", "v70a")


def upgrade():
    quoted = ", ".join(f"'{value}'" for value in CATALOG_SOURCES)
    op.execute(f"DELETE FROM external_identifiers WHERE source IN ({quoted})")


def downgrade():
    # Removed aliases remain available in catalog_runs and raw_catalog_rows,
    # but cannot be reconstructed safely as authoritative target aliases.
    pass
