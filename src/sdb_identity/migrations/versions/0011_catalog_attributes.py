"""Versioned structured attributes from selected catalog rows."""

from alembic import op

from sdb_identity.models import Base

revision = "0011_catalog_attributes"
down_revision = "0010_catalog_identifier_policy"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["catalog_attributes"]],
    )
    op.execute(
        "CREATE VIEW current_catalog_attributes AS "
        "SELECT a.*, t.sdbid FROM catalog_attributes a "
        "JOIN catalog_runs r ON r.id=a.run_id "
        "JOIN targets t ON t.id=a.target_id "
        "WHERE r.is_current=1 AND r.status='match'"
    )
    op.execute(
        "CREATE VIEW catalog_attribute_conflicts AS "
        "SELECT t.sdbid, a.key, COUNT(DISTINCT a.provider) AS provider_count, "
        "COUNT(DISTINCT COALESCE(a.value_text, CAST(a.value_float AS TEXT))) AS value_count "
        "FROM catalog_attributes a JOIN catalog_runs r ON r.id=a.run_id "
        "JOIN targets t ON t.id=a.target_id "
        "WHERE r.is_current=1 AND r.status='match' "
        "GROUP BY t.sdbid, a.key HAVING provider_count > 1 AND value_count > 1"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS catalog_attribute_conflicts")
    op.execute("DROP VIEW IF EXISTS current_catalog_attributes")
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables["catalog_attributes"]],
    )
