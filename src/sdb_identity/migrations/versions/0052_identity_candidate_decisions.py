"""Make identity candidate selection a decision-only projection."""

from alembic import op
import sqlalchemy as sa


revision = "0052_identity_candidate_decisions"
down_revision = "0051_catalog_result_decisions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP VIEW IF EXISTS ambiguous_matches")
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            "match_candidates"
        )
    }
    if "accepted" in columns:
        # Historical application writes already added decisions. Preserve any
        # older accepted flag that predates that invariant before dropping the
        # mutable duplicate state.
        op.execute(
            "INSERT INTO match_decisions "
            "(candidate_id,decision,method,actor,reason,created_at) "
            "SELECT candidate.id,'accepted','migration',NULL,"
            "'migrated accepted candidate flag',CURRENT_TIMESTAMP "
            "FROM match_candidates AS candidate "
            "WHERE candidate.accepted=1 AND NOT EXISTS ("
            "SELECT 1 FROM match_decisions AS decision "
            "WHERE decision.candidate_id=candidate.id "
            "AND decision.decision='accepted')"
        )
        with op.batch_alter_table("match_candidates") as batch:
            batch.drop_column("accepted")
    indexes = {
        value["name"]
        for value in sa.inspect(op.get_bind()).get_indexes(
            "match_decisions"
        )
    }
    if "ix_match_decisions_candidate_id" not in indexes:
        op.create_index(
            "ix_match_decisions_candidate_id",
            "match_decisions",
            ["candidate_id"],
        )
    if "ix_match_decisions_decision" not in indexes:
        op.create_index(
            "ix_match_decisions_decision",
            "match_decisions",
            ["decision"],
        )
    op.execute(
        "CREATE VIEW ambiguous_matches AS "
        "SELECT candidate.submission_id,COUNT(*) AS candidate_count,"
        "MAX(candidate.score) AS best_score "
        "FROM match_candidates AS candidate "
        "GROUP BY candidate.submission_id "
        "HAVING NOT EXISTS ("
        "SELECT 1 FROM match_decisions AS decision "
        "JOIN match_candidates AS selected "
        "ON selected.id=decision.candidate_id "
        "WHERE selected.submission_id=candidate.submission_id "
        "AND decision.decision='accepted')"
    )


def downgrade():
    raise RuntimeError("development schema migrations are forward-only")
