"""separate IRAS source families from target reconciliation

Revision ID: 0002_iras_source_families
Revises: 0001_current_schema
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_iras_source_families"
down_revision = "0001_current_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP VIEW current_iras_band_selections")
    op.create_table(
        "iras_source_families",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("psc_detection_id", sa.Integer(), nullable=False),
        sa.Column("fsc_detection_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=40), nullable=False),
        sa.Column("normalized_separation", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["psc_detection_id"], ["catalog_detections.id"]),
        sa.ForeignKeyConstraint(["fsc_detection_id"], ["catalog_detections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("psc_detection_id"),
        sa.UniqueConstraint("fsc_detection_id"),
        sa.UniqueConstraint("psc_detection_id", "fsc_detection_id"),
    )
    with op.batch_alter_table("iras_source_families") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_iras_source_families_psc_detection_id"),
            ["psc_detection_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_iras_source_families_fsc_detection_id"),
            ["fsc_detection_id"],
            unique=False,
        )

    with op.batch_alter_table("iras_detection_families") as batch_op:
        batch_op.add_column(sa.Column("source_family_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_iras_detection_families_source_family_id",
            "iras_source_families",
            ["source_family_id"],
            ["id"],
        )
        batch_op.create_index(
            batch_op.f("ix_iras_detection_families_source_family_id"),
            ["source_family_id"],
            unique=False,
        )

    op.execute("""
        INSERT OR IGNORE INTO iras_source_families (
            psc_detection_id, fsc_detection_id, method,
            normalized_separation, reason, created_at
        )
        SELECT pr.detection_id, fr.detection_id, 'migrated_reconciliation',
               f.normalized_separation, f.reason, f.created_at
        FROM iras_detection_families f
        JOIN raw_catalog_rows pr
          ON pr.run_id = f.psc_run_id AND pr.accepted = 1
        JOIN raw_catalog_rows fr
          ON fr.run_id = f.fsc_run_id AND fr.accepted = 1
        WHERE f.status = 'associated'
    """)
    op.execute("""
        UPDATE iras_detection_families
        SET source_family_id = (
            SELECT sf.id
            FROM iras_source_families sf
            JOIN raw_catalog_rows pr
              ON pr.run_id = iras_detection_families.psc_run_id
             AND pr.accepted = 1
            JOIN raw_catalog_rows fr
              ON fr.run_id = iras_detection_families.fsc_run_id
             AND fr.accepted = 1
            WHERE sf.psc_detection_id = pr.detection_id
              AND sf.fsc_detection_id = fr.detection_id
            LIMIT 1
        )
        WHERE status = 'associated'
    """)

    op.create_table(
        "iras_band_selections_new",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("band", sa.String(length=30), nullable=False),
        sa.Column("selected_measurement_id", sa.Integer(), nullable=False),
        sa.Column("alternate_measurement_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["family_id"], ["iras_source_families.id"]),
        sa.ForeignKeyConstraint(
            ["selected_measurement_id"], ["normalized_measurements.id"]
        ),
        sa.ForeignKeyConstraint(
            ["alternate_measurement_id"], ["normalized_measurements.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("family_id", "band"),
    )
    op.execute("""
        INSERT OR IGNORE INTO iras_band_selections_new (
            family_id, band, selected_measurement_id,
            alternate_measurement_id, method, reason
        )
        SELECT f.source_family_id, s.band, s.selected_measurement_id,
               s.alternate_measurement_id, s.method, s.reason
        FROM iras_band_selections s
        JOIN iras_detection_families f ON f.id = s.family_id
        WHERE f.source_family_id IS NOT NULL
        ORDER BY f.is_current DESC, f.id DESC
    """)
    op.drop_table("iras_band_selections")
    op.rename_table("iras_band_selections_new", "iras_band_selections")
    with op.batch_alter_table("iras_band_selections") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_iras_band_selections_family_id"),
            ["family_id"], unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_iras_band_selections_alternate_measurement_id"),
            ["alternate_measurement_id"], unique=False,
        )

    op.create_table(
        "iras_family_target_association_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("reviewed_run_id", sa.Integer(), nullable=False),
        sa.Column("reviewed_raw_row_id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["family_id"], ["iras_source_families.id"]),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"]),
        sa.ForeignKeyConstraint(["reviewed_run_id"], ["catalog_runs.id"]),
        sa.ForeignKeyConstraint(["reviewed_raw_row_id"], ["raw_catalog_rows.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("iras_family_target_association_actions") as batch_op:
        for column in (
            "family_id", "target_id", "action", "reviewed_run_id",
            "reviewed_raw_row_id",
        ):
            batch_op.create_index(batch_op.f(
                f"ix_iras_family_target_association_actions_{column}"
            ), [column], unique=False)
        batch_op.create_index(
            "ix_iras_family_target_association_actions_pair",
            ["target_id", "family_id"], unique=False,
        )

    with op.batch_alter_table(
        "catalog_target_association_actions", recreate="always",
    ) as batch_op:
        batch_op.add_column(
            sa.Column("family_action_id", sa.Integer()),
            insert_before="actor",
        )
        batch_op.create_foreign_key(
            "fk_catalog_target_association_actions_family_action_id",
            "iras_family_target_association_actions",
            ["family_action_id"], ["id"],
        )
        batch_op.create_index(
            batch_op.f("ix_catalog_target_association_actions_family_action_id"),
            ["family_action_id"], unique=False,
        )

    op.execute("""
        CREATE VIEW current_iras_band_selections AS
        SELECT r.target_id, r.status, r.normalized_separation, s.*
        FROM iras_detection_families r
        JOIN iras_band_selections s ON s.family_id = r.source_family_id
        WHERE r.is_current = 1
    """)


def downgrade():
    raise NotImplementedError("development schema migrations are forward-only")
