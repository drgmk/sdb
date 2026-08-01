from __future__ import annotations

import sqlite3

from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.database import init_database
from sdb_identity.database import make_session_factory
from sdb_identity.models import NormalizedMeasurement
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement


def test_migration_builds_schema_and_views(tmp_path):
    path = tmp_path / "fresh.sqlite"
    init_database(path)
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert version is not None
    assert "operator_actions" not in tables
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
        alma_member_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(alma_members)")
        }
    assert version == "0053_alma_member_canonical"
    assert {
        "measurement_eligibility_actions",
        "dataset_revisions", "curated_records",
        "curated_association_actions",
        "reference_application_runs", "reference_application_items",
        "reference_application_records",
        "catalog_result_decisions", "catalog_retry_actions",
        "catalog_attributes",
        "export_dirty_targets",
        "samples", "sample_membership_actions", "sample_export_runs",
        "sample_export_items",
        "catalog_batch_requests",
        "alma_sync_runs", "alma_sync_chunks", "alma_members",
        "alma_member_positions",
        "hierarchy_sources", "hierarchy_records", "target_systems",
        "target_system_members",
        "measurement_target_associations", "hierarchy_match_candidates",
        "hierarchy_match_actions",
        "target_lifecycle_actions", "measurement_association_actions",
        "catalog_detections",
        "catalog_target_association_actions",
        "structural_edges", "structural_edge_actions",
    } <= tables
    assert "alma_observations" not in tables
    assert "positions_json" not in alma_member_columns
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
        "measurement_eligibility_history",
        "unresolved_curated_records",
        "curated_association_history",
        "reference_application_status", "unmatched_reference_records",
        "catalog_result_decision_history",
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


def test_catalog_result_migration_restores_reviewed_acquisition_run(tmp_path):
    path = tmp_path / "catalog-result-upgrade.sqlite"
    init_database(path, "0050_measurement_eligibility")
    sessions = make_session_factory(path)
    target = IdentityService(sessions).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    service = CatalogAcquisitionService(sessions, {
        "2mass": FakeCatalog([
            candidate("one", ra=10.00010, measurements=[measurement()]),
            candidate("two", ra=10.00011, measurements=[measurement()]),
        ]),
    })
    acquired = service.refresh(target.sdbid, "2mass")

    with sqlite3.connect(path) as connection:
        chosen = connection.execute(
            "SELECT id,detection_id,source_id FROM raw_catalog_rows "
            "WHERE run_id=? ORDER BY id DESC LIMIT 1",
            (acquired.run_id,),
        ).fetchone()
        connection.execute(
            "UPDATE catalog_runs SET is_current=0 WHERE id=?",
            (acquired.run_id,),
        )
        replacement_id = connection.execute(
            "INSERT INTO catalog_runs "
            "(target_id,batch_request_id,provider,release,status,is_current,"
            "query_ra_deg,query_dec_deg,query_epoch,candidate_count,"
            "selected_source_id,error,created_at,completed_at) "
            "SELECT target_id,batch_request_id,provider,release,'match',1,"
            "query_ra_deg,query_dec_deg,query_epoch,candidate_count,?,error,"
            "created_at,completed_at FROM catalog_runs WHERE id=? RETURNING id",
            (chosen[2], acquired.run_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO raw_catalog_rows "
            "(run_id,detection_id,source_id,ra_deg,dec_deg,epoch,"
            "separation_arcsec,score,accepted,payload_json) "
            "SELECT ?,detection_id,source_id,ra_deg,dec_deg,epoch,"
            "separation_arcsec,score,1,payload_json FROM raw_catalog_rows "
            "WHERE id=?",
            (replacement_id, chosen[0]),
        )
        connection.execute(
            "INSERT INTO catalog_match_overrides "
            "(target_id,provider,previous_run_id,replacement_run_id,action,"
            "selected_source_id,actor,reason,created_at) "
            "VALUES (?, '2mass', ?, ?, 'accept_candidate', ?, "
            "'legacy reviewer', 'legacy selection', CURRENT_TIMESTAMP)",
            (target.target_id, acquired.run_id, replacement_id, chosen[2]),
        )
        connection.commit()

    init_database(path)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        views = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            )
        }
        current = connection.execute(
            "SELECT id FROM catalog_runs WHERE is_current=1"
        ).fetchall()
        decision = connection.execute(
            "SELECT reviewed_run_id,action,reviewed_raw_row_id,reason "
            "FROM catalog_result_decisions"
        ).fetchone()

    assert "catalog_match_overrides" not in tables
    assert "catalog_match_override_history" not in views
    assert current == [(acquired.run_id,)]
    assert decision == (
        acquired.run_id,
        "accept_detection",
        chosen[0],
        "legacy selection",
    )


def test_identity_decision_migration_preserves_legacy_accepted_flag(tmp_path):
    path = tmp_path / "identity-decision-upgrade.sqlite"
    init_database(path, "0051_catalog_result_decisions")
    sessions = make_session_factory(path)
    target = IdentityService(sessions).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO submissions "
            "(id,target_id,input_name,input_epoch,status,created_at) "
            "VALUES (100,?,'legacy identity',2000,'completed',CURRENT_TIMESTAMP)",
            (target.target_id,),
        )
        connection.execute(
            "INSERT INTO match_candidates "
            "(id,submission_id,provider,source_id,ra_deg,dec_deg,epoch,"
            "proper_motion_available,separation_arcsec,score,score_details) "
            "VALUES (100,100,'gaia_dr3','123',10,-20,2016,0,0.1,0.9,'{}')"
        )
        connection.execute(
            "ALTER TABLE match_candidates ADD COLUMN accepted BOOLEAN "
            "NOT NULL DEFAULT 0"
        )
        connection.execute(
            "UPDATE match_candidates SET accepted=1 WHERE id=100"
        )
        connection.commit()

    init_database(path)
    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(match_candidates)"
            )
        }
        decision = connection.execute(
            "SELECT candidate_id,decision,method,reason "
            "FROM match_decisions WHERE candidate_id=100"
        ).fetchone()

    assert "accepted" not in columns
    assert decision == (
        100,
        "accepted",
        "migration",
        "migrated accepted candidate flag",
    )


def test_provider_scope_migration_repairs_import_order_overwrite(tmp_path):
    path = tmp_path / "scope-repair.sqlite"
    init_database(path)
    sessions = make_session_factory(path)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    CatalogAcquisitionService(sessions, {
        "ubvmeans": FakeCatalog(
            [candidate(
                "+100000123|m_LID=D",
                measurements=[measurement("VJ")],
            )],
            name="ubvmeans",
            release="II/168",
            query_epoch=2000.0,
        ),
    }).refresh(target.sdbid, "ubvmeans")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE normalized_measurements "
            "SET ownership_scope='shared', blend_state='blended', "
            "blend_reason='duplicate_source'"
        )
        connection.execute(
            "UPDATE alembic_version "
            "SET version_num='0045_catalog_target_associations'"
        )
        connection.commit()

    init_database(path)

    with sessions() as session:
        value = session.query(NormalizedMeasurement).one()
        assert (
            value.ownership_scope,
            value.blend_state,
            value.blend_reason,
        ) == (
            "system",
            "blended",
            "catalog_multiple_in_aperture",
        )


def test_tdsc_scope_migration_repairs_import_order_overwrite(tmp_path):
    path = tmp_path / "tdsc-scope-repair.sqlite"
    init_database(path)
    sessions = make_session_factory(path)
    target = IdentityService(sessions).add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    CatalogAcquisitionService(sessions, {
        "tdsc": FakeCatalog(
            [candidate(
                "88|m_TDSC=A",
                measurements=[measurement("VT")],
            )],
            name="tdsc",
            release="I/276",
            query_epoch=2000.0,
        ),
    }).refresh(target.sdbid, "tdsc")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE normalized_measurements "
            "SET ownership_scope='shared', blend_state='blended', "
            "blend_reason='duplicate_source'"
        )
        connection.execute(
            "UPDATE alembic_version "
            "SET version_num='0046_restore_provider_photometry_scope'"
        )
        connection.commit()

    init_database(path)

    with sessions() as session:
        value = session.query(NormalizedMeasurement).one()
        assert (
            value.ownership_scope,
            value.blend_state,
            value.blend_reason,
        ) == ("component", "clear", None)


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
    assert version == "0053_alma_member_canonical"
    assert identifiers == [("HD 1", "simbad"), ("Preferred name", "manual")]
