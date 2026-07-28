"""Generalize catalog match overrides to catalog-result review actions."""

from alembic import op
from sqlalchemy import inspect


revision = "0043_catalog_result_actions"
down_revision = "0042_drop_operator_actions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP VIEW IF EXISTS catalog_match_override_history")
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns(
            "catalog_match_overrides"
        )
    }
    # Fresh databases created by historical migrations use the current model
    # metadata and already have this shape. Existing databases need a small,
    # explicit SQLite rebuild to make selected_source_id nullable.
    if "action" not in columns:
        op.execute(
            "CREATE TABLE catalog_match_overrides_0043 ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "target_id INTEGER NOT NULL REFERENCES targets(id), "
            "provider VARCHAR(40) NOT NULL, "
            "previous_run_id INTEGER NOT NULL REFERENCES catalog_runs(id), "
            "replacement_run_id INTEGER NOT NULL REFERENCES catalog_runs(id), "
            "action VARCHAR(30) NOT NULL, "
            "selected_source_id VARCHAR(200), "
            "actor VARCHAR(100) NOT NULL, "
            "reason TEXT NOT NULL, "
            "created_at DATETIME NOT NULL)"
        )
        op.execute(
            "INSERT INTO catalog_match_overrides_0043 "
            "(id, target_id, provider, previous_run_id, replacement_run_id, "
            "action, selected_source_id, actor, reason, created_at) "
            "SELECT id, target_id, provider, previous_run_id, replacement_run_id, "
            "'accept_candidate', selected_source_id, actor, reason, created_at "
            "FROM catalog_match_overrides"
        )
        op.drop_table("catalog_match_overrides")
        op.rename_table(
            "catalog_match_overrides_0043", "catalog_match_overrides",
        )
        for column in (
            "target_id", "provider", "previous_run_id", "replacement_run_id",
            "action",
        ):
            op.create_index(
                f"ix_catalog_match_overrides_{column}",
                "catalog_match_overrides",
                [column],
            )
    op.execute(
        "CREATE VIEW catalog_match_override_history AS "
        "SELECT o.id, t.sdbid, o.provider, o.action, o.previous_run_id, "
        "o.replacement_run_id, o.selected_source_id, o.actor, o.reason, "
        "o.created_at FROM catalog_match_overrides o "
        "JOIN targets t ON t.id=o.target_id"
    )


def downgrade():
    raise NotImplementedError(
        "0043 permits reviewed no-match actions without a selected source"
    )
