from __future__ import annotations

import sqlite3

from sqlalchemy import inspect

from sdb_identity.database import init_database, make_engine
from sdb_identity.models import Base


CURRENT_REVISION = "0001_current_schema"

CURRENT_VIEWS = {
    "alma_archive_status",
    "alma_sync_chunk_status",
    "ambiguous_matches",
    "blend_review",
    "catalog_attribute_conflicts",
    "catalog_batch_status",
    "catalog_result_decision_history",
    "catalog_status",
    "curated_association_history",
    "current_alma_projects",
    "current_catalog_attributes",
    "current_iras_band_selections",
    "current_measurement_assignments",
    "current_photometry",
    "current_sample_memberships",
    "current_simbad_metadata",
    "current_target_lifecycle",
    "failed_provider_requests",
    "hierarchy_graph_effective",
    "hierarchy_match_action_history",
    "hierarchy_match_review",
    "hierarchy_relationship_summary",
    "hierarchy_system_members",
    "import_job_status",
    "import_run_status",
    "measurement_assignment_history",
    "measurement_eligibility_history",
    "metadata_status",
    "pending_export_targets",
    "reference_application_status",
    "sample_export_summary",
    "sample_summary",
    "target_notes",
    "target_summary",
    "unmatched_reference_records",
    "unresolved_curated_records",
    "unresolved_submissions",
}


def test_baseline_builds_current_schema_and_views(tmp_path):
    path = tmp_path / "fresh.sqlite"
    init_database(path)

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        views = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            )
        }

    assert version == CURRENT_REVISION
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    assert views == CURRENT_VIEWS
    assert {
        "operator_actions",
        "alma_observations",
        "catalog_match_overrides",
    }.isdisjoint(tables)


def test_baseline_matches_model_columns_indexes_and_foreign_keys(tmp_path):
    path = tmp_path / "model-schema.sqlite"
    init_database(path)
    inspector = inspect(make_engine(path))

    for name, table in Base.metadata.tables.items():
        assert [
            (
                column["name"],
                str(column["type"]),
                column["nullable"],
                column["primary_key"],
            )
            for column in inspector.get_columns(name)
        ] == [
            (
                column.name,
                str(column.type),
                column.nullable,
                column.primary_key,
            )
            for column in table.columns
        ]
        assert {index["name"] for index in inspector.get_indexes(name)} == {
            index.name for index in table.indexes
        }
        assert {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(name)
        } == {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }

        actual_foreign_keys = {
            (
                tuple(key["constrained_columns"]),
                key["referred_table"],
                tuple(key["referred_columns"]),
            )
            for key in inspector.get_foreign_keys(name)
        }
        model_foreign_keys = {
            (
                tuple(column.name for column in constraint.columns),
                next(iter(constraint.elements)).column.table.name,
                tuple(element.column.name for element in constraint.elements),
            )
            for constraint in table.foreign_key_constraints
        }
        assert actual_foreign_keys == model_foreign_keys


def test_baseline_preserves_defaults_and_composite_indexes(tmp_path):
    path = tmp_path / "schema-details.sqlite"
    init_database(path)

    with sqlite3.connect(path) as connection:
        defaults = {
            (table, row[1]): row[4]
            for table in (
                "catalog_detections",
                "hierarchy_match_candidates",
                "structural_edges",
                "target_system_members",
                "target_systems",
            )
            for row in connection.execute(f"PRAGMA table_info({table})")
            if row[4] is not None
        }

        index_columns = {
            name: tuple(
                row[2]
                for row in connection.execute(f"PRAGMA index_info({name})")
            )
            for name in (
                "ix_alma_member_positions_dec_ra",
                "ix_alma_member_positions_fov_dec_ra",
                "ix_normalized_measurements_provider_source",
            )
        }

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert defaults == {
        ("catalog_detections", "normalization_status"): "'pending'",
        ("hierarchy_match_candidates", "status"): "'candidate'",
        ("structural_edges", "direction"): "'pair'",
        ("structural_edges", "structural_role"): "'non_structural'",
        ("structural_edges", "status"): "'derived'",
        ("structural_edges", "confidence"): "'unknown'",
        ("structural_edges", "note"): "''",
        ("structural_edges", "reason"): "''",
        ("target_system_members", "source"): "'manual'",
        ("target_systems", "source"): "'manual'",
    }
    assert index_columns["ix_alma_member_positions_dec_ra"] == (
        "dec_deg",
        "ra_deg",
    )
    assert index_columns["ix_alma_member_positions_fov_dec_ra"] == (
        "fov_deg",
        "dec_deg",
        "ra_deg",
    )
    assert index_columns["ix_normalized_measurements_provider_source"] == (
        "provider",
        "source_id",
    )
