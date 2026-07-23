"""Canonicalize catalog detections separately from target query encounters."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0036_canonical_catalog_detections"
down_revision = "0035_system_photometry_foundation"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # SQLite implements batch alterations by renaming and rebuilding the
    # table. Views referring to that table make the temporary rename fail, so
    # remove them for the duration of the migration and recreate them below
    # using the new encounter-aware joins.
    op.execute("DROP VIEW IF EXISTS blend_review")
    op.execute("DROP VIEW IF EXISTS current_photometry")
    tables = set(inspect(bind).get_table_names())
    if "catalog_detections" not in tables:
        op.create_table(
            "catalog_detections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider", sa.String(40), nullable=False, index=True),
            sa.Column("release", sa.String(100), nullable=False),
            sa.Column("detection_key", sa.String(240), nullable=False),
            sa.Column("source_id", sa.String(200), nullable=False, index=True),
            sa.Column("ra_deg", sa.Float(), nullable=False),
            sa.Column("dec_deg", sa.Float(), nullable=False),
            sa.Column("epoch", sa.Float(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("provider", "release", "detection_key"),
        )

    raw_columns = {column["name"] for column in inspect(bind).get_columns("raw_catalog_rows")}
    if "detection_id" not in raw_columns:
        with op.batch_alter_table("raw_catalog_rows") as batch:
            batch.add_column(sa.Column("detection_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_raw_catalog_rows_detection_id",
                "catalog_detections", ["detection_id"], ["id"],
            )
            batch.create_index("ix_raw_catalog_rows_detection_id", ["detection_id"])

    measurement_columns = {
        column["name"] for column in inspect(bind).get_columns("normalized_measurements")
    }
    if "detection_id" not in measurement_columns:
        with op.batch_alter_table("normalized_measurements") as batch:
            batch.add_column(sa.Column("detection_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("measurement_key", sa.String(100), nullable=True))
            batch.create_foreign_key(
                "fk_normalized_measurements_detection_id",
                "catalog_detections", ["detection_id"], ["id"],
            )
            batch.create_index("ix_normalized_measurements_detection_id", ["detection_id"])

    # Existing databases retain each old raw row as a distinct legacy
    # detection. A clean rebuild receives cross-target canonicalization; this
    # migration does not guess whether historical rows with the same source ID
    # really represented the same epoch/observation.
    op.execute(
        "INSERT INTO catalog_detections "
        "(provider, release, detection_key, source_id, ra_deg, dec_deg, epoch, payload_json, created_at) "
        "SELECT c.provider, c.release, 'legacy-raw:' || r.id, r.source_id, "
        "r.ra_deg, r.dec_deg, r.epoch, r.payload_json, CURRENT_TIMESTAMP "
        "FROM raw_catalog_rows r JOIN catalog_runs c ON c.id=r.run_id "
        "WHERE r.detection_id IS NULL"
    )
    op.execute(
        "UPDATE raw_catalog_rows SET detection_id=("
        "SELECT d.id FROM catalog_detections d "
        "WHERE d.detection_key='legacy-raw:' || raw_catalog_rows.id) "
        "WHERE detection_id IS NULL"
    )
    op.execute(
        "UPDATE normalized_measurements SET detection_id=("
        "SELECT r.detection_id FROM raw_catalog_rows r "
        "WHERE r.id=normalized_measurements.raw_row_id), "
        "measurement_key=band || ':legacy:' || id "
        "WHERE detection_id IS NULL"
    )
    with op.batch_alter_table("raw_catalog_rows") as batch:
        batch.alter_column("detection_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("normalized_measurements") as batch:
        batch.alter_column("detection_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("measurement_key", existing_type=sa.String(100), nullable=False)
        batch.create_unique_constraint(
            "uq_normalized_measurements_detection_measurement",
            ["detection_id", "measurement_key"],
        )

    op.execute(
        "CREATE VIEW current_photometry AS "
        "SELECT m.*, r.target_id AS encounter_target_id "
        "FROM normalized_measurements m "
        "JOIN raw_catalog_rows raw ON raw.detection_id=m.detection_id "
        "JOIN catalog_runs r ON r.id=raw.run_id "
        "WHERE r.is_current=1 AND r.status='match' AND raw.accepted=1"
    )
    op.execute(
        "CREATE VIEW blend_review AS "
        "SELECT m.*, t.sdbid, r.target_id AS encounter_target_id "
        "FROM normalized_measurements m "
        "JOIN raw_catalog_rows raw ON raw.detection_id=m.detection_id "
        "JOIN catalog_runs r ON r.id=raw.run_id "
        "JOIN targets t ON t.id=r.target_id "
        "WHERE r.is_current=1 AND r.status='match' AND raw.accepted=1 "
        "AND (m.blend_status!='clear' OR m.association_scope!='component')"
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS blend_review")
    op.execute("DROP VIEW IF EXISTS current_photometry")
    with op.batch_alter_table("normalized_measurements") as batch:
        batch.drop_constraint(
            "uq_normalized_measurements_detection_measurement", type_="unique"
        )
        batch.drop_index("ix_normalized_measurements_detection_id")
        batch.drop_constraint("fk_normalized_measurements_detection_id", type_="foreignkey")
        batch.drop_column("measurement_key")
        batch.drop_column("detection_id")
    with op.batch_alter_table("raw_catalog_rows") as batch:
        batch.drop_index("ix_raw_catalog_rows_detection_id")
        batch.drop_constraint("fk_raw_catalog_rows_detection_id", type_="foreignkey")
        batch.drop_column("detection_id")
    op.drop_table("catalog_detections")
    op.execute(
        "CREATE VIEW current_photometry AS "
        "SELECT m.* FROM normalized_measurements m "
        "JOIN catalog_runs r ON r.id=m.run_id WHERE r.is_current=1 AND r.status='match'"
    )
    op.execute(
        "CREATE VIEW blend_review AS "
        "SELECT m.*, t.sdbid FROM normalized_measurements m "
        "JOIN catalog_runs r ON r.id=m.run_id "
        "JOIN targets t ON t.id=m.target_id "
        "WHERE r.is_current=1 AND r.status='match' "
        "AND (m.blend_status!='clear' OR m.association_scope!='component')"
    )
