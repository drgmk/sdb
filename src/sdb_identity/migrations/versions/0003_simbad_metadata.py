"""Versioned SIMBAD metadata, relationships, and user notes."""

from alembic import op

from sdb_identity.models import Base

revision = "0003_simbad_metadata"
down_revision = "0002_catalog_photometry"
branch_labels = None
depends_on = None


TABLES = (
    "metadata_runs",
    "simbad_metadata",
    "simbad_object_types",
    "simbad_relationships",
    "user_notes",
)


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in TABLES],
    )
    op.execute(
        "CREATE VIEW metadata_status AS "
        "SELECT r.id AS run_id, t.sdbid, r.provider, r.release, r.status, "
        "r.query_identifier, r.candidate_count, r.error, r.completed_at "
        "FROM metadata_runs r JOIN targets t ON t.id=r.target_id WHERE r.is_current=1"
    )
    op.execute(
        "CREATE VIEW current_simbad_metadata AS "
        "SELECT m.* FROM simbad_metadata m JOIN metadata_runs r ON r.id=m.run_id "
        "WHERE r.is_current=1 AND r.status='match'"
    )
    op.execute(
        "CREATE VIEW target_notes AS "
        "SELECT n.id, t.sdbid, n.actor, n.text, n.created_at "
        "FROM user_notes n JOIN targets t ON t.id=n.target_id"
    )


def downgrade():
    for view in ("target_notes", "current_simbad_metadata", "metadata_status"):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in reversed(TABLES)],
    )
