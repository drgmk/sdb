"""Hierarchy match candidates."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_hierarchy_match_candidates"
down_revision = "0026_hierarchy_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hierarchy_match_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("hierarchy_records.id"), nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("match_method", sa.String(length=80), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("separation_arcsec", sa.Float()),
        sa.Column("identifier", sa.String(length=200)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="candidate"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("record_id", "target_id"),
    )
    op.create_index("ix_hierarchy_match_candidates_record_id", "hierarchy_match_candidates", ["record_id"])
    op.create_index("ix_hierarchy_match_candidates_target_id", "hierarchy_match_candidates", ["target_id"])
    op.create_index("ix_hierarchy_match_candidates_provider", "hierarchy_match_candidates", ["provider"])
    op.create_index("ix_hierarchy_match_candidates_status", "hierarchy_match_candidates", ["status"])
    op.execute(
        "CREATE VIEW hierarchy_match_review AS "
        "SELECT c.id AS candidate_id, c.provider, c.status, c.match_method, c.score, "
        "c.separation_arcsec, c.identifier, c.reason, "
        "r.id AS record_id, r.source_id, r.native_id, r.component, r.discoverer_id, "
        "r.ra_deg AS record_ra_deg, r.dec_deg AS record_dec_deg, "
        "r.separation_arcsec AS hierarchy_separation_arcsec, r.pa_deg, r.measure_epoch, "
        "t.id AS target_id, t.sdbid, t.ra2000_deg, t.dec2000_deg, "
        "s.release AS source_release "
        "FROM hierarchy_match_candidates c "
        "JOIN hierarchy_records r ON r.id = c.record_id "
        "JOIN targets t ON t.id = c.target_id "
        "JOIN hierarchy_sources s ON s.id = r.source_id"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS hierarchy_match_review")
    op.drop_index("ix_hierarchy_match_candidates_status", table_name="hierarchy_match_candidates")
    op.drop_index("ix_hierarchy_match_candidates_provider", table_name="hierarchy_match_candidates")
    op.drop_index("ix_hierarchy_match_candidates_target_id", table_name="hierarchy_match_candidates")
    op.drop_index("ix_hierarchy_match_candidates_record_id", table_name="hierarchy_match_candidates")
    op.drop_table("hierarchy_match_candidates")
