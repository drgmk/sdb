"""Initial identity core schema."""

from alembic import op
import sqlalchemy as sa

from sdb_identity.models import Base

revision = "0001_identity_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    initial_tables = [
        Base.metadata.tables[name]
        for name in (
            "targets", "submissions", "astrometric_solutions",
            "external_identifiers", "provider_outcomes", "match_candidates",
            "match_decisions",
        )
    ]
    Base.metadata.create_all(bind=bind, tables=initial_tables)
    # Historical untyped audit table, retired by migration 0042.  It is
    # declared here rather than in current ORM metadata.
    op.create_table(
        "operator_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("object_type", sa.String(40), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("CREATE VIEW target_summary AS SELECT t.id, t.sdbid, t.ra2000_deg, t.dec2000_deg, a.source AS astrometry_source, a.proper_motion_available FROM targets t LEFT JOIN astrometric_solutions a ON a.id=t.canonical_astrometry_id")
    op.execute("CREATE VIEW unresolved_submissions AS SELECT * FROM submissions WHERE status='failed' OR target_id IS NULL")
    op.execute("CREATE VIEW ambiguous_matches AS SELECT submission_id, COUNT(*) AS candidate_count, MAX(score) AS best_score FROM match_candidates GROUP BY submission_id HAVING SUM(CASE WHEN accepted THEN 1 ELSE 0 END)=0")
    op.execute("CREATE VIEW failed_provider_requests AS SELECT * FROM provider_outcomes WHERE status IN ('transient_failure','permanent_failure')")


def downgrade():
    for view in ("failed_provider_requests", "ambiguous_matches", "unresolved_submissions", "target_summary"):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.drop_table("operator_actions")
    initial_tables = [
        Base.metadata.tables[name]
        for name in (
            "match_decisions", "match_candidates",
            "provider_outcomes", "external_identifiers", "submissions",
            "astrometric_solutions", "targets",
        )
    ]
    Base.metadata.drop_all(bind=op.get_bind(), tables=initial_tables)
