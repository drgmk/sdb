from __future__ import annotations

import json
import pytest
from sqlalchemy import delete, select

from sdb_identity.catalog_associations import (
    catalog_coverage_by_target,
    catalog_target_candidates,
)
from sdb_identity.catalog_measurements import current_measurements_for_target
from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.database import init_database, make_session_factory
from sdb_identity.decision_history import system_decision_history
from sdb_identity.hierarchy import HierarchyService
from sdb_identity.models.catalogs import (
    CatalogDetection,
    CatalogDetectionProvenance,
    CatalogRun,
    CatalogTargetAssociationAction,
    NormalizedMeasurement,
    RawCatalogRow,
)
from sdb_identity.review_actions import (
    review_catalog_target_association_decision,
)
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement


def test_catalog_coverage_distinguishes_direct_results_from_shared_evidence(
    session_factory,
):
    identity = IdentityService(session_factory)
    parent = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    component = identity.add(
        AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
    )
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("component", ra=10.0 + 4.1 / 3600.0, dec=0.0),
        ]),
    }).refresh(parent.sdbid, "2mass")

    with session_factory() as session:
        rows = catalog_coverage_by_target(
            session,
            [parent.target_id, component.target_id],
            providers=("2mass", "tycho2"),
        )

    by_target = {row["target_sdbid"]: row for row in rows}
    assert by_target[parent.sdbid]["current_providers"] == ["2mass"]
    assert by_target[parent.sdbid]["missing_providers"] == ["tycho2"]
    assert by_target[component.sdbid]["current_providers"] == []
    assert by_target[component.sdbid]["missing_providers"] == [
        "2mass", "tycho2",
    ]


def test_catalog_coverage_offers_backfill_for_missing_detection_provenance(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10.0, dec_deg=0.0)
    )
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate(measurements=[measurement()]),
        ]),
    }).refresh(target.sdbid, "2mass")
    with session_factory.begin() as session:
        session.execute(delete(CatalogDetectionProvenance))
    with session_factory() as session:
        row = catalog_coverage_by_target(
            session, [target.target_id], providers=("2mass",),
        )[0]

    assert row["normalization_gaps"] == [{
        "detection_id": row["normalization_gaps"][0]["detection_id"],
        "provider": "2mass",
        "source_id": "J00400000-2000000",
        "status": "missing_provenance",
        "error": None,
    }]


def test_catalog_target_candidates_reconcile_detection_to_later_component(
    session_factory,
):
    identity = IdentityService(session_factory)
    parent = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    result = CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("parent", ra=10.0, dec=0.0),
            candidate(
                "component",
                ra=10.0 + 4.1 / 3600.0,
                dec=0.0,
                measurements=[measurement("2MJ"), measurement("2MH")],
            ),
        ]),
    }).refresh(parent.sdbid, "2mass")
    assert result.status == "match"
    component = identity.add(
        AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
    )

    with session_factory() as session:
        rows = catalog_target_candidates(
            session, [parent.target_id, component.target_id],
        )

    component_row = next(
        row for row in rows
        if (
            row["source_id"] == "component"
            and row["target_id"] == component.target_id
        )
    )
    parent_encounter = next(
        row for row in rows
        if row["source_id"] == "component" and row["target_id"] == parent.target_id
    )

    assert component_row["association_status"] == "strong_candidate"
    assert component_row["association_basis"] == "close position"
    assert component_row["separation_arcsec"] == pytest.approx(0.1, abs=0.01)
    assert component_row["encounter_sdbids"] == [parent.sdbid]
    assert component_row["measurement_count"] == 2
    assert component_row["measurement_bands"] == ["2MH", "2MJ"]
    assert parent_encounter["association_status"] == "candidate"


def test_catalog_target_association_is_audited_without_rewriting_query_provenance(
    session_factory, monkeypatch,
):
    monkeypatch.setenv("SDB_ACTOR", "catalog reviewer")
    identity = IdentityService(session_factory)
    parent = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    component = identity.add(
        AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
    )
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("parent", ra=10.0, dec=0.0),
            candidate(
                "component",
                ra=10.0 + 4.1 / 3600.0,
                dec=0.0,
                measurements=[measurement("2MJ"), measurement("2MH")],
            ),
        ]),
    }).refresh(parent.sdbid, "2mass")

    with session_factory() as session:
        candidate_row = next(
            row for row in catalog_target_candidates(
                session, [parent.target_id, component.target_id],
            )
            if (
                row["target_id"] == component.target_id
                and row["source_id"] == "component"
            )
        )
        run_state = list(session.execute(
            select(
                CatalogRun.id,
                CatalogRun.status,
                CatalogRun.is_current,
                CatalogRun.selected_source_id,
            ).order_by(CatalogRun.id)
        ))
        raw_state = list(session.execute(
            select(
                RawCatalogRow.id,
                RawCatalogRow.run_id,
                RawCatalogRow.accepted,
            ).order_by(RawCatalogRow.id)
        ))

    preview = review_catalog_target_association_decision(
        session_factory,
        target_reference=component.sdbid,
        detection_id=int(candidate_row["detection_id"]),
        action="accept",
        reviewed_raw_row_id=int(candidate_row["representative_raw_row_id"]),
    )
    assert preview["has_changes"] is True
    assert preview["suggested_reason"].startswith("Accepted 2mass source")
    applied = review_catalog_target_association_decision(
        session_factory,
        target_reference=component.sdbid,
        detection_id=int(candidate_row["detection_id"]),
        action="accept",
        reviewed_raw_row_id=int(candidate_row["representative_raw_row_id"]),
        apply=True,
        reason=None,
        expected_token=str(preview["state_token"]),
    )
    assert applied["applied"]["actions_added"] == 1

    with session_factory() as session:
        assert list(session.execute(
            select(
                CatalogRun.id,
                CatalogRun.status,
                CatalogRun.is_current,
                CatalogRun.selected_source_id,
            ).order_by(CatalogRun.id)
        )) == run_state
        assert list(session.execute(
            select(
                RawCatalogRow.id,
                RawCatalogRow.run_id,
                RawCatalogRow.accepted,
            ).order_by(RawCatalogRow.id)
        )) == raw_state
        rows = catalog_target_candidates(
            session, [parent.target_id, component.target_id],
        )
        associated = next(
            row for row in rows
            if (
                row["target_id"] == component.target_id
                and row["source_id"] == "component"
            )
        )
        assert associated["association_status"] == "accepted"
        assert associated["association_basis"] == "manual review"
        assert len(
            current_measurements_for_target(session, component.target_id)
        ) == 2
        actions = list(session.scalars(
            select(CatalogTargetAssociationAction)
        ))
        assert [(row.action, row.actor) for row in actions] == [
            ("accept", "catalog reviewer")
        ]
    history = system_decision_history(
        session_factory, component.sdbid, include_system=False,
    )
    assert any(
        row["domain"] == "catalog_target_association"
        and row["action"] == "accept"
        for row in history
    )

    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate(
                "component",
                ra=10.0 + 4.1 / 3600.0,
                dec=0.0,
                measurements=[measurement("2MJ"), measurement("2MH")],
            ),
            candidate("parent", ra=10.0, dec=0.0),
        ]),
    }).refresh(parent.sdbid, "2mass")
    with session_factory() as session:
        refreshed = next(
            row for row in catalog_target_candidates(
                session, [parent.target_id, component.target_id],
            )
            if (
                row["target_id"] == component.target_id
                and row["source_id"] == "component"
            )
        )
        assert refreshed["association_status"] == "accepted"
        assert len(
            current_measurements_for_target(session, component.target_id)
        ) == 2

    reject_preview = review_catalog_target_association_decision(
        session_factory,
        target_reference=component.sdbid,
        detection_id=int(candidate_row["detection_id"]),
        action="reject",
        reviewed_raw_row_id=int(candidate_row["representative_raw_row_id"]),
    )
    review_catalog_target_association_decision(
        session_factory,
        target_reference=component.sdbid,
        detection_id=int(candidate_row["detection_id"]),
        action="reject",
        reviewed_raw_row_id=int(candidate_row["representative_raw_row_id"]),
        apply=True,
        expected_token=str(reject_preview["state_token"]),
    )
    with session_factory() as session:
        assert current_measurements_for_target(
            session, component.target_id,
        ) == []
        rows = catalog_target_candidates(
            session, [parent.target_id, component.target_id],
        )
        rejected = next(
            row for row in rows
            if (
                row["target_id"] == component.target_id
                and row["source_id"] == "component"
            )
        )
        assert rejected["association_status"] == "rejected"


def test_system_scale_catalog_centroid_does_not_create_component_match(
    session_factory,
):
    identity = IdentityService(session_factory)
    parent = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    component = identity.add(
        AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
    )
    CatalogAcquisitionService(session_factory, {
        "iras_fsc": FakeCatalog(
            [
                candidate("system", ra=10.0, dec=0.0),
                candidate("near-component", ra=10.0 + 4.1 / 3600.0, dec=0.0),
            ],
            name="iras_fsc",
            release="fake-iras-fsc",
        ),
    }).refresh(parent.sdbid, "iras_fsc")

    with session_factory() as session:
        rows = catalog_target_candidates(
            session, [parent.target_id, component.target_id],
        )

    assert any(
        row["source_id"] == "system"
        and row["target_id"] == parent.target_id
        and row["association_status"] == "current_match"
        for row in rows
    )
    assert not any(
        row["source_id"] == "near-component"
        and row["target_id"] == component.target_id
        for row in rows
    )


def test_catalog_target_candidate_result_is_independent_of_component_import_order(
    tmp_path,
):
    def scenario(name: str, *, component_first: bool):
        path = tmp_path / f"{name}.sqlite"
        init_database(path)
        sessions = make_session_factory(path)
        identity = IdentityService(sessions)
        if component_first:
            component = identity.add(
                AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
            )
            parent = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
        else:
            parent = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
            component = None
        CatalogAcquisitionService(sessions, {
            "2mass": FakeCatalog([
                candidate("parent", ra=10.0, dec=0.0),
                candidate("component", ra=10.0 + 4.1 / 3600.0, dec=0.0),
            ]),
        }).refresh(parent.sdbid, "2mass")
        if component is None:
            component = identity.add(
                AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
            )
        context = HierarchyService(sessions).system_context(component.sdbid)
        row = next(
            value for value in context["catalog_target_candidates"]
            if (
                value["source_id"] == "component"
                and value["target_sdbid"] == component.sdbid
            )
        )
        return {
            "source_id": row["source_id"],
            "target_sdbid": row["target_sdbid"],
            "association_status": row["association_status"],
            "association_basis": row["association_basis"],
            "separation_arcsec": row["separation_arcsec"],
            "encounter_sdbids": row["encounter_sdbids"],
        }

    parent_first = scenario("parent-first", component_first=False)
    component_first = scenario("component-first", component_first=True)

    assert parent_first == component_first
    assert parent_first["association_status"] == "strong_candidate"


def test_detection_measurements_are_independent_of_provider_query_order(
    tmp_path,
):
    def scenario(name: str, order: tuple[str, str]):
        path = tmp_path / f"{name}.sqlite"
        init_database(path)
        sessions = make_session_factory(path)
        identity = IdentityService(sessions)
        targets = {
            "parent": identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0)),
            "component": identity.add(
                AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
            ),
        }
        service = CatalogAcquisitionService(sessions, {
            "2mass": FakeCatalog([
                candidate(
                    "parent",
                    ra=10.0,
                    dec=0.0,
                    measurements=[measurement("2MJ", value=7.0)],
                ),
                candidate(
                    "component",
                    ra=10.0 + 4.1 / 3600.0,
                    dec=0.0,
                    measurements=[
                        measurement("2MJ", value=8.0),
                        measurement("2MH", value=7.5),
                    ],
                ),
            ]),
        })
        for key in order:
            service.refresh(targets[key].sdbid, "2mass")
        with sessions() as session:
            detection = session.scalar(select(CatalogDetection).where(
                CatalogDetection.source_id == "component"
            ))
            values = list(session.scalars(
                select(NormalizedMeasurement)
                .where(NormalizedMeasurement.detection_id == detection.id)
                .order_by(NormalizedMeasurement.band)
            ))
            rows = catalog_target_candidates(
                session,
                [
                    targets["parent"].target_id,
                    targets["component"].target_id,
                ],
            )
        component_row = next(
            row for row in rows
            if (
                row["source_id"] == "component"
                and row["target_sdbid"] == targets["component"].sdbid
            )
        )
        return {
            "measurements": [
                (value.band, value.value, value.unit) for value in values
            ],
            "association_status": component_row["association_status"],
            "measurement_bands": component_row["measurement_bands"],
            "encounter_sdbids": component_row["encounter_sdbids"],
        }

    parent_first = scenario(
        "query-parent-first", ("parent", "component")
    )
    component_first = scenario(
        "query-component-first", ("component", "parent")
    )

    assert parent_first == component_first
    assert parent_first["measurements"] == [
        ("2MH", 7.5, "mag"),
        ("2MJ", 8.0, "mag"),
    ]


def test_identifier_backed_encounter_outranks_position_only_encounter(
    session_factory,
):
    identity = IdentityService(session_factory)
    composite = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    component = identity.add(AddRequest(ra_deg=10.0003, dec_deg=0.0))
    service = CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate(
                "same", ra=10.0, dec=0.0,
                measurements=[measurement("2MJ")],
            ),
        ]),
    })
    service.refresh(composite.sdbid, "2mass")
    service.refresh(component.sdbid, "2mass")
    with session_factory.begin() as session:
        rows = list(session.scalars(select(RawCatalogRow).order_by(RawCatalogRow.id)))
        for raw in rows:
            payload = json.loads(raw.payload_json)
            run = session.get(CatalogRun, raw.run_id)
            agrees = run.target_id == composite.target_id
            payload["_sdb_association"] = {
                "method": "position+identifier" if agrees else "position",
                "identifier_agreement": agrees,
            }
            raw.payload_json = json.dumps(payload)

    with session_factory() as session:
        assert len(current_measurements_for_target(
            session, composite.target_id,
        )) == 1
        assert current_measurements_for_target(session, component.target_id) == []

    with session_factory() as session:
        raw = session.scalar(
            select(RawCatalogRow)
            .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
            .where(CatalogRun.target_id == component.target_id)
        )
        detection_id = raw.detection_id
        raw_id = raw.id
    preview = review_catalog_target_association_decision(
        session_factory,
        target_reference=component.sdbid,
        detection_id=detection_id,
        action="accept",
        reviewed_raw_row_id=raw_id,
    )
    review_catalog_target_association_decision(
        session_factory,
        target_reference=component.sdbid,
        detection_id=detection_id,
        action="accept",
        reviewed_raw_row_id=raw_id,
        apply=True,
        actor="test",
        expected_token=str(preview["state_token"]),
    )
    with session_factory() as session:
        assert len(current_measurements_for_target(
            session, component.target_id,
        )) == 1


def test_tdsc_component_identifier_outranks_system_identifier(
    session_factory,
):
    identity = IdentityService(session_factory)
    composite = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    component_a = identity.add(AddRequest(ra_deg=10.0003, dec_deg=0.0))
    service = CatalogAcquisitionService(session_factory, {
        "tdsc": FakeCatalog(
            [candidate(
                "88|m_TDSC=A",
                ra=10.0003,
                dec=0.0,
                measurements=[measurement("VT")],
            )],
            name="tdsc",
            release="I/276",
            query_epoch=2000.0,
        ),
    })
    service.refresh(composite.sdbid, "tdsc")
    service.refresh(component_a.sdbid, "tdsc")
    with session_factory.begin() as session:
        for raw in session.scalars(select(RawCatalogRow)):
            payload = json.loads(raw.payload_json)
            run = session.get(CatalogRun, raw.run_id)
            matched = (
                ["HD 224953"]
                if run.target_id == composite.target_id
                else ["TYC 9134-1714-1"]
            )
            payload["m_TDSC"] = "A"
            payload["_sdb_association"] = {
                "method": "position+identifier",
                "identifier_agreement": True,
                "matched_identifiers": matched,
            }
            raw.payload_json = json.dumps(payload)

    with session_factory() as session:
        assert current_measurements_for_target(
            session, composite.target_id,
        ) == []
        assert len(current_measurements_for_target(
            session, component_a.target_id,
        )) == 1
