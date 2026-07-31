"""Separate catalog acquisition evidence from operator result decisions."""

from alembic import op
import sqlalchemy as sa

revision = "0051_catalog_result_decisions"
down_revision = "0050_measurement_eligibility"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("catalog_result_decisions"):
        op.create_table(
            "catalog_result_decisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("reviewed_run_id", sa.Integer(), sa.ForeignKey("catalog_runs.id"), nullable=False),
            sa.Column("action", sa.String(length=30), nullable=False),
            sa.Column("accepted_detection_id", sa.Integer(), sa.ForeignKey("catalog_detections.id")),
            sa.Column("reviewed_raw_row_id", sa.Integer(), sa.ForeignKey("raw_catalog_rows.id")),
            sa.Column("actor", sa.String(length=100), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "action IN ('accept_detection', 'reviewed_no_match')",
                name="ck_catalog_result_decision_action",
            ),
            sa.CheckConstraint(
                "(action = 'accept_detection' AND accepted_detection_id IS NOT NULL "
                "AND reviewed_raw_row_id IS NOT NULL) OR "
                "(action = 'reviewed_no_match' AND accepted_detection_id IS NULL "
                "AND reviewed_raw_row_id IS NULL)",
                name="ck_catalog_result_decision_evidence",
            ),
        )
        for column in ("target_id", "provider", "reviewed_run_id", "action", "accepted_detection_id", "reviewed_raw_row_id"):
            op.create_index(f"ix_catalog_result_decisions_{column}", "catalog_result_decisions", [column])
        op.create_index(
            "ix_catalog_result_decisions_run_order",
            "catalog_result_decisions",
            ["reviewed_run_id", "id"],
        )
    if not inspector.has_table("catalog_retry_actions"):
        op.create_table(
            "catalog_retry_actions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("failed_run_id", sa.Integer(), sa.ForeignKey("catalog_runs.id"), nullable=False),
            sa.Column("retry_run_id", sa.Integer(), sa.ForeignKey("catalog_runs.id"), nullable=False),
            sa.Column("actor", sa.String(length=100), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ("target_id", "provider", "failed_run_id", "retry_run_id"):
            op.create_index(f"ix_catalog_retry_actions_{column}", "catalog_retry_actions", [column])

    if inspector.has_table("catalog_match_overrides"):
        op.execute(
            "INSERT INTO catalog_result_decisions "
            "(id,target_id,provider,reviewed_run_id,action,accepted_detection_id,reviewed_raw_row_id,actor,reason,created_at) "
            "SELECT override.id,override.target_id,override.provider,override.previous_run_id,"
            "CASE WHEN override.action='accept_candidate' THEN 'accept_detection' ELSE 'reviewed_no_match' END,"
            "selected.detection_id,original.id,override.actor,override.reason,override.created_at "
            "FROM catalog_match_overrides AS override "
            "LEFT JOIN raw_catalog_rows AS selected ON selected.run_id=override.replacement_run_id AND selected.accepted=1 "
            "LEFT JOIN raw_catalog_rows AS original ON original.id=(SELECT candidate.id FROM raw_catalog_rows AS candidate "
            "WHERE candidate.run_id=override.previous_run_id AND candidate.detection_id=selected.detection_id ORDER BY candidate.id LIMIT 1) "
            "WHERE override.action IN ('accept_candidate','reviewed_no_match')"
        )
        op.execute(
            "INSERT INTO catalog_retry_actions (target_id,provider,failed_run_id,retry_run_id,actor,reason,created_at) "
            "SELECT target_id,provider,previous_run_id,replacement_run_id,actor,reason,created_at "
            "FROM catalog_match_overrides WHERE action='retry'"
        )
        # An accept/no-match override used to replace the provider result with
        # a synthetic current run.  Restore the original acquisition run only
        # when that replacement is still current; a later real refresh must
        # remain authoritative.  The copied run and rows remain non-current
        # historical data because other provenance rows may reference them.
        op.execute(
            "UPDATE catalog_runs SET is_current=1 WHERE id IN ("
            "SELECT override.previous_run_id FROM catalog_match_overrides AS override "
            "JOIN catalog_runs AS replacement ON replacement.id=override.replacement_run_id "
            "WHERE override.action IN ('accept_candidate','reviewed_no_match') "
            "AND replacement.is_current=1)"
        )
        op.execute(
            "UPDATE catalog_runs SET is_current=0 WHERE id IN ("
            "SELECT replacement_run_id FROM catalog_match_overrides "
            "WHERE action IN ('accept_candidate','reviewed_no_match'))"
        )
        op.execute("DROP VIEW IF EXISTS catalog_match_override_history")
        op.drop_table("catalog_match_overrides")
    op.execute(
        "CREATE VIEW IF NOT EXISTS catalog_result_decision_history AS "
        "SELECT decision.id,target.sdbid,decision.provider,decision.action,decision.reviewed_run_id,"
        "decision.accepted_detection_id,decision.reviewed_raw_row_id,decision.actor,decision.reason,decision.created_at "
        "FROM catalog_result_decisions AS decision JOIN targets AS target ON target.id=decision.target_id"
    )


def downgrade():
    raise RuntimeError("development schema migrations are forward-only")
