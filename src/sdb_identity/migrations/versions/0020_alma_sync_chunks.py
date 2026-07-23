"""Durable and resumable ALMA synchronization chunks."""

from alembic import op

from sdb_identity.models import Base


revision = "0020_alma_sync_chunks"
down_revision = "0019_alma_archive"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["alma_sync_chunks"]],
    )
    op.execute(
        "CREATE VIEW alma_sync_chunk_status AS "
        "SELECT c.*, r.mode AS run_mode, r.status AS run_status "
        "FROM alma_sync_chunks c JOIN alma_sync_runs r ON r.id=c.run_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS alma_sync_chunk_status")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["alma_sync_chunks"]],
    )
