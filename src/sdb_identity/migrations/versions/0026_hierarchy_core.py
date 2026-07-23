"""Generic target hierarchy core."""

from alembic import op
import sqlalchemy as sa


revision = "0026_hierarchy_core"
down_revision = "0025_alma_member_status"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hierarchy_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False, index=True),
        sa.Column("release", sa.String(100), nullable=False),
        sa.Column("source_file", sa.Text()),
        sa.Column("checksum", sa.String(128)),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text()),
    )
    op.create_table(
        "hierarchy_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("hierarchy_sources.id"), nullable=False, index=True),
        sa.Column("provider", sa.String(40), nullable=False, index=True),
        sa.Column("native_id", sa.String(200), nullable=False, index=True),
        sa.Column("component", sa.String(40), index=True),
        sa.Column("discoverer_id", sa.String(100), index=True),
        sa.Column("ra_deg", sa.Float()),
        sa.Column("dec_deg", sa.Float()),
        sa.Column("first_epoch", sa.Float()),
        sa.Column("last_epoch", sa.Float()),
        sa.Column("measure_epoch", sa.Float()),
        sa.Column("separation_arcsec", sa.Float()),
        sa.Column("pa_deg", sa.Float()),
        sa.Column("magnitude_primary", sa.Float()),
        sa.Column("magnitude_secondary", sa.Float()),
        sa.Column("delta_mag", sa.Float()),
        sa.Column("raw_payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "target_systems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("primary_target_id", sa.Integer(), sa.ForeignKey("targets.id")),
        sa.Column("source", sa.String(40), nullable=False, server_default="manual"),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "target_system_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("system_id", sa.Integer(), sa.ForeignKey("target_systems.id"), nullable=False, index=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False, index=True),
        sa.Column("component_label", sa.String(40)),
        sa.Column("source", sa.String(40), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("system_id", "target_id", "component_label"),
    )
    op.create_table(
        "target_relationships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("system_id", sa.Integer(), sa.ForeignKey("target_systems.id"), index=True),
        sa.Column("parent_target_id", sa.Integer(), sa.ForeignKey("targets.id"), index=True),
        sa.Column("child_target_id", sa.Integer(), sa.ForeignKey("targets.id"), index=True),
        sa.Column("primary_target_id", sa.Integer(), sa.ForeignKey("targets.id"), index=True),
        sa.Column("secondary_target_id", sa.Integer(), sa.ForeignKey("targets.id"), index=True),
        sa.Column("relationship_type", sa.String(40), nullable=False),
        sa.Column("component", sa.String(40)),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_record_id", sa.Integer(), sa.ForeignKey("hierarchy_records.id")),
        sa.Column("separation_arcsec", sa.Float()),
        sa.Column("pa_deg", sa.Float()),
        sa.Column("relation_epoch", sa.Float()),
        sa.Column("confidence", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column("status", sa.String(30), nullable=False, server_default="current", index=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "measurement_target_associations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("measurement_id", sa.Integer(), sa.ForeignKey("normalized_measurements.id"), nullable=False, index=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id"), nullable=False, index=True),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("method", sa.String(40), nullable=False),
        sa.Column("weight", sa.Float()),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("measurement_id", "target_id", "role"),
    )
    op.execute(
        "CREATE VIEW hierarchy_relationship_summary AS "
        "SELECT r.id, r.relationship_type, r.source, r.status, r.confidence, "
        "r.component, r.separation_arcsec, r.pa_deg, r.relation_epoch, "
        "p.sdbid AS parent_sdbid, c.sdbid AS child_sdbid, "
        "a.sdbid AS primary_sdbid, b.sdbid AS secondary_sdbid, "
        "s.name AS system_name, r.reason, r.actor, r.created_at "
        "FROM target_relationships r "
        "LEFT JOIN targets p ON p.id=r.parent_target_id "
        "LEFT JOIN targets c ON c.id=r.child_target_id "
        "LEFT JOIN targets a ON a.id=r.primary_target_id "
        "LEFT JOIN targets b ON b.id=r.secondary_target_id "
        "LEFT JOIN target_systems s ON s.id=r.system_id"
    )
    op.execute(
        "CREATE VIEW hierarchy_system_members AS "
        "SELECT s.id AS system_id, s.name AS system_name, s.source AS system_source, "
        "m.component_label, t.id AS target_id, t.sdbid, t.ra2000_deg, t.dec2000_deg, "
        "m.source AS member_source, m.created_at "
        "FROM target_system_members m "
        "JOIN target_systems s ON s.id=m.system_id "
        "JOIN targets t ON t.id=m.target_id"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS hierarchy_system_members")
    op.execute("DROP VIEW IF EXISTS hierarchy_relationship_summary")
    op.drop_table("measurement_target_associations")
    op.drop_table("target_relationships")
    op.drop_table("target_system_members")
    op.drop_table("target_systems")
    op.drop_table("hierarchy_records")
    op.drop_table("hierarchy_sources")
