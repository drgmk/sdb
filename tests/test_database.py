from __future__ import annotations

import sqlite3

from sdb_identity.database import init_database


def test_migration_builds_schema_and_views(tmp_path):
    path = tmp_path / "fresh.sqlite"
    init_database(path)
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert version is not None
    assert {"target_summary", "unresolved_submissions", "ambiguous_matches", "failed_provider_requests"} <= views


def test_existing_identity_database_upgrades_to_catalog_schema(tmp_path):
    path = tmp_path / "upgrade.sqlite"
    init_database(path, "0001_identity_core")
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "catalog_runs" not in tables

    init_database(path, "0002_catalog_photometry")
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert version == "0002_catalog_photometry"
    assert {"catalog_runs", "raw_catalog_rows", "normalized_measurements"} <= tables
    assert {"catalog_status", "current_photometry"} <= views


def test_catalog_database_upgrades_to_metadata_schema(tmp_path):
    path = tmp_path / "metadata-upgrade.sqlite"
    init_database(path, "0002_catalog_photometry")
    init_database(path, "0003_simbad_metadata")
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert version == "0003_simbad_metadata"
    assert {"metadata_runs", "simbad_metadata", "simbad_object_types", "simbad_relationships", "user_notes"} <= tables
    assert {"metadata_status", "current_simbad_metadata", "target_notes"} <= views


def test_metadata_database_upgrades_to_batch_schema(tmp_path):
    path = tmp_path / "batch-upgrade.sqlite"
    init_database(path, "0003_simbad_metadata")
    init_database(path, "0004_batch_ingestion")
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert version == "0004_batch_ingestion"
    assert {"import_runs", "import_items", "import_jobs"} <= tables
    assert {"import_run_status", "import_job_status"} <= views


def test_batch_database_upgrades_to_photometry_override_schema(tmp_path):
    path = tmp_path / "override-upgrade.sqlite"
    init_database(path, "0004_batch_ingestion")
    init_database(path)
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        match_candidate_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(match_candidates)")
        }
        simbad_metadata_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(simbad_metadata)")
        }
    assert version == "0041_photometry_semantics"
    assert {
        "photometry_overrides",
        "dataset_revisions", "curated_records",
        "curated_association_actions",
        "curated_photometry_overrides",
        "reference_application_runs", "reference_application_items",
        "reference_application_records",
        "catalog_match_overrides",
        "catalog_attributes",
        "export_dirty_targets",
        "samples", "sample_membership_actions", "sample_export_runs",
        "sample_export_items",
        "catalog_batch_requests",
        "alma_sync_runs", "alma_sync_chunks", "alma_observations", "alma_members",
        "alma_member_positions",
        "hierarchy_sources", "hierarchy_records", "target_systems",
        "target_system_members",
        "measurement_target_associations", "hierarchy_match_candidates",
        "hierarchy_match_actions",
        "target_lifecycle_actions", "measurement_association_actions",
        "catalog_detections",
        "structural_edges", "structural_edge_actions",
    } <= tables
    assert {
        "pm_ra_cosdec_masyr", "pm_dec_masyr", "proper_motion_available",
        "parallax_mas", "radial_velocity_kms", "position_bibcode",
        "proper_motion_bibcode", "parallax_bibcode",
        "radial_velocity_bibcode",
    } <= match_candidate_columns
    assert {
        "pm_ra_cosdec_masyr", "pm_dec_masyr", "proper_motion_bibcode",
    } <= simbad_metadata_columns
    assert {
        "photometry_override_history",
        "unresolved_curated_records",
        "curated_association_history",
        "curated_photometry_override_history",
        "reference_application_status", "unmatched_reference_records",
        "catalog_match_override_history",
        "current_catalog_attributes", "catalog_attribute_conflicts",
        "pending_export_targets",
        "current_sample_memberships", "sample_summary", "sample_export_summary",
        "catalog_batch_status",
        "alma_archive_status", "alma_sync_chunk_status", "current_alma_projects",
        "hierarchy_relationship_summary", "hierarchy_system_members",
        "hierarchy_match_review", "hierarchy_match_action_history",
        "current_target_lifecycle", "measurement_assignment_history",
        "current_measurement_assignments",
    } <= views


def test_catalog_identifier_policy_removes_promoted_aliases_only(tmp_path):
    path = tmp_path / "identifier-policy.sqlite"
    init_database(path, "0009_catalog_match_overrides")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO targets "
            "(id, sdbid, ra2000_deg, dec2000_deg, canonical_astrometry_id, created_at) "
            "VALUES (1, 'sdbid-v3-test', 10, -20, NULL, '2026-01-01')"
        )
        rows = [
            (1, 1, "2MASS J1", "2MASS J1", "2mass"),
            (2, 1, "AllWISE J1", "ALLWISE J1", "allwise"),
            (3, 1, "HD 1", "HD 1", "simbad"),
            (4, 1, "Preferred name", "PREFERRED NAME", "manual"),
        ]
        connection.executemany(
            "INSERT INTO external_identifiers "
            "(id, target_id, value, normalized_value, source) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    init_database(path)
    with sqlite3.connect(path) as connection:
        identifiers = list(connection.execute(
            "SELECT value, source FROM external_identifiers ORDER BY id"
        ))
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == "0041_photometry_semantics"
    assert identifiers == [("HD 1", "simbad"), ("Preferred name", "manual")]
