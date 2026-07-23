"""Catalog runs, raw rows, and normalized photometry."""

from alembic import op

from sdb_identity.models import Base

revision = "0002_catalog_photometry"
down_revision = "0001_identity_core"
branch_labels = None
depends_on = None


TABLES = ("catalog_runs", "raw_catalog_rows", "normalized_measurements")


def upgrade():
    tables = [Base.metadata.tables[name] for name in TABLES]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables)
    op.execute(
        "CREATE VIEW catalog_status AS "
        "SELECT r.id AS run_id, t.sdbid, r.provider, r.release, r.status, "
        "r.candidate_count, r.selected_source_id, r.error, r.completed_at "
        "FROM catalog_runs r JOIN targets t ON t.id=r.target_id WHERE r.is_current=1"
    )
    op.execute(
        "CREATE VIEW current_photometry AS "
        "SELECT m.* FROM normalized_measurements m "
        "JOIN catalog_runs r ON r.id=m.run_id WHERE r.is_current=1 AND r.status='match'"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS current_photometry")
    op.execute("DROP VIEW IF EXISTS catalog_status")
    tables = [Base.metadata.tables[name] for name in reversed(TABLES)]
    Base.metadata.drop_all(bind=op.get_bind(), tables=tables)
