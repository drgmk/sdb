from __future__ import annotations

import pytest
from sqlalchemy import select

from sdb_identity.catalogs import CatalogService, MeasurementValue
from sdb_identity.models import (
    MeasurementAssociationAction,
    MeasurementTargetAssociation,
    NormalizedMeasurement,
    PhotometryOverride,
    TargetLifecycleAction,
)
from sdb_identity.photometry import assign_measurement_target
from sdb_identity.review_actions import (
    review_detection_decision,
    review_photometry_eligibility_decision,
    review_target_lifecycle_decision,
)
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate
from tests.test_system_photometry_foundation import _configured_system


def _wise_measurements(
    session_factory,
    target,
    *,
    excluded_band=None,
    source_id="review-wise",
    ra=10.0,
    dec=-20.0,
):
    values = [
        MeasurementValue(
            band=band, value=value, error=0.03, unit="mag",
            excluded=band == excluded_band,
            exclusion_reason="provider quality" if band == excluded_band else None,
        )
        for band, value in (("WISE3P4", 6.1), ("WISE22", 5.2))
    ]
    CatalogService(session_factory, {"allwise": FakeCatalog(
        [candidate(source_id, ra=ra, dec=dec, measurements=values)],
        name="allwise", release="test", query_epoch=2010.5,
    )}).refresh(target.sdbid, "allwise")
    with session_factory() as session:
        rows = list(session.scalars(
            select(NormalizedMeasurement)
            .where(NormalizedMeasurement.target_id == target.target_id)
            .order_by(NormalizedMeasurement.band)
        ))
    return rows


def test_fit_eligibility_review_appends_atomic_band_overrides(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    _wise_measurements(session_factory, target, excluded_band="WISE22")
    changes = [
        {
            "target": target.sdbid,
            "provider": "allwise",
            "band": "WISE22",
            "excluded": False,
        },
        {
            "target": target.sdbid,
            "provider": "allwise",
            "band": "WISE3P4",
            "excluded": True,
        },
    ]

    preview = review_photometry_eligibility_decision(
        session_factory, changes=changes,
    )
    assert preview["has_changes"] is True
    assert "Excluded" in preview["suggested_reason"]
    assert "Included" in preview["suggested_reason"]
    assert {row["desired_excluded"] for row in preview["changes"]} == {False, True}

    applied = review_photometry_eligibility_decision(
        session_factory,
        changes=changes,
        apply=True,
        actor="reviewer",
        reason="inspected quality flags",
        expected_token=preview["state_token"],
    )
    assert applied["applied"]["overrides_added"] == 2
    with session_factory() as session:
        overrides = list(session.scalars(
            select(PhotometryOverride).order_by(PhotometryOverride.band)
        ))
    assert [(row.band, row.excluded) for row in overrides] == [
        ("WISE22", False),
        ("WISE3P4", True),
    ]

    repeated = review_photometry_eligibility_decision(
        session_factory, changes=changes,
    )
    assert repeated["has_changes"] is False


def test_fit_eligibility_review_can_store_suggested_reason(
    session_factory, monkeypatch,
):
    monkeypatch.setenv("SDB_ACTOR", "reviewer")
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    _wise_measurements(session_factory, target)
    changes = [{
        "target": target.sdbid,
        "provider": "allwise",
        "band": "WISE3P4",
        "excluded": True,
    }]
    preview = review_photometry_eligibility_decision(
        session_factory, changes=changes,
    )
    review_photometry_eligibility_decision(
        session_factory,
        changes=changes,
        apply=True,
        expected_token=preview["state_token"],
    )
    with session_factory() as session:
        override = session.scalar(select(PhotometryOverride))
    assert override.actor == "reviewer"
    assert override.reason == preview["suggested_reason"]


def test_detection_decision_previews_and_atomically_assigns_all_selected_bands(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    measurements = _wise_measurements(
        session_factory, system, excluded_band="WISE3P4",
    )
    for measurement in measurements:
        assign_measurement_target(
            session_factory, measurement.id, system.sdbid,
            role="composite_scope", method="manual", actor="test",
            reason="known AB scope",
        )

    preview = review_detection_decision(
        session_factory,
        detection_id=measurements[0].detection_id,
        scope_target_reference=system.sdbid,
        contributor_references=[component_a.sdbid, component_b.sdbid],
        include_composite_scope=True,
    )

    assert preview["mode"] == "preview"
    assert preview["has_changes"] is True
    assert len(preview["add_assignments"]) == 4
    assert preview["remove_assignments"] == []
    assert {row["excluded"] for row in preview["measurements"]} == {False, True}

    applied = review_detection_decision(
        session_factory,
        detection_id=measurements[0].detection_id,
        scope_target_reference=system.sdbid,
        contributor_references=[component_a.sdbid, component_b.sdbid],
        include_composite_scope=True,
        apply=True,
        actor="reviewer",
        reason="WISE detection covers A and B",
        expected_token=preview["state_token"],
    )

    assert applied["applied"] == {
        "lifecycle_actions": 0,
        "assignments_added": 4,
        "assignments_removed": 0,
    }
    with session_factory() as session:
        associations = list(session.scalars(select(MeasurementTargetAssociation)))
        actions = list(session.scalars(select(MeasurementAssociationAction)))
        stored = list(session.scalars(select(NormalizedMeasurement)))
    assert len(associations) == 6
    assert len(actions) == 6
    assert sum(row.method == "review_ui_detection" for row in associations) == 4
    assert {row.band: row.excluded for row in stored}["WISE3P4"] is True

    repeated = review_detection_decision(
        session_factory,
        detection_id=measurements[0].detection_id,
        scope_target_reference=system.sdbid,
        contributor_references=[component_a.sdbid, component_b.sdbid],
        include_composite_scope=True,
        apply=True,
        actor="reviewer",
        reason="idempotent repeat",
    )
    assert repeated["has_changes"] is False
    assert repeated["applied"]["assignments_added"] == 0


def test_detection_decision_can_convert_scope_to_physical_contributor(
    session_factory,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    measurements = _wise_measurements(session_factory, target)
    selected = measurements[0]
    assign_measurement_target(
        session_factory, selected.id, target.sdbid,
        role="composite_scope", method="automatic_proposal", actor="test",
        reason="pre-review proposal",
    )

    preview = review_detection_decision(
        session_factory,
        detection_id=selected.detection_id,
        scope_target_reference=target.sdbid,
        contributor_references=[target.sdbid],
        include_composite_scope=False,
        measurement_ids=[selected.id],
        target_role="physical",
        target_state="active",
    )

    assert preview["lifecycle_change"]["to_role"] == "physical"
    assert len(preview["add_assignments"]) == 1
    assert len(preview["remove_assignments"]) == 1
    assert [row["measurement_id"] for row in preview["measurements"]] == [selected.id]

    review_detection_decision(
        session_factory,
        detection_id=selected.detection_id,
        scope_target_reference=target.sdbid,
        contributor_references=[target.sdbid],
        include_composite_scope=False,
        measurement_ids=[selected.id],
        target_role="physical",
        target_state="active",
        apply=True,
        actor="reviewer",
        reason="confirmed physical target",
        expected_token=preview["state_token"],
    )
    with session_factory() as session:
        associations = list(session.scalars(select(MeasurementTargetAssociation)))
        lifecycle = list(session.scalars(select(TargetLifecycleAction)))
    assert [(row.measurement_id, row.role) for row in associations] == [
        (selected.id, "contributor")
    ]
    assert (lifecycle[-1].role, lifecycle[-1].state) == ("physical", "active")


def test_detection_decision_rejects_stale_preview_token(session_factory):
    system, component_a, component_b = _configured_system(session_factory)
    measurement = _wise_measurements(session_factory, system)[0]
    preview = review_detection_decision(
        session_factory,
        detection_id=measurement.detection_id,
        scope_target_reference=system.sdbid,
        contributor_references=[component_a.sdbid],
        include_composite_scope=True,
        measurement_ids=[measurement.id],
    )
    assign_measurement_target(
        session_factory, measurement.id, component_b.sdbid,
        role="contributor", method="manual", actor="other reviewer",
        reason="changed after preview",
    )

    with pytest.raises(RuntimeError, match="review state changed"):
        review_detection_decision(
            session_factory,
            detection_id=measurement.detection_id,
            scope_target_reference=system.sdbid,
            contributor_references=[component_a.sdbid],
            include_composite_scope=True,
            measurement_ids=[measurement.id],
            apply=True,
            actor="reviewer",
            reason="stale browser action",
            expected_token=preview["state_token"],
        )


def test_target_lifecycle_review_distinguishes_model_role_from_multiplicity(
    session_factory,
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    measurement = _wise_measurements(session_factory, target)[0]
    assign_measurement_target(
        session_factory,
        measurement.id,
        target.sdbid,
        role="composite_scope",
        method="automatic_proposal",
        actor="test",
        reason="accepted before lifecycle review",
    )
    preview = review_target_lifecycle_decision(
        session_factory,
        target_reference=target.sdbid,
        role="physical",
        state="active",
    )
    assert preview["interpretation"]["model_target"] is True
    assert "multiplicity" in preview["interpretation"]["multiplicity"]
    assert preview["has_changes"] is True
    assert preview["assignment_reconciliation"] == [{
        "association_id": preview["assignment_reconciliation"][0]["association_id"],
        "measurement_id": measurement.id,
        "target_id": target.target_id,
        "sdbid": target.sdbid,
        "from_role": "composite_scope",
        "to_role": "contributor",
        "add_contributor": True,
    }]

    applied = review_target_lifecycle_decision(
        session_factory,
        target_reference=target.sdbid,
        role="physical",
        state="active",
        apply=True,
        actor="reviewer",
        reason="represent unresolved AB with one combined-light model",
        expected_token=preview["state_token"],
    )
    assert applied["applied"]["lifecycle_actions"] == 1
    assert applied["applied"]["assignments_removed"] == 1
    assert applied["applied"]["assignments_added"] == 1
    with session_factory() as session:
        lifecycle = list(session.scalars(select(TargetLifecycleAction)))
        associations = list(session.scalars(select(MeasurementTargetAssociation)))
    assert (lifecycle[-1].role, lifecycle[-1].state) == ("physical", "active")
    assert [(row.target_id, row.role) for row in associations] == [
        (target.target_id, "contributor")
    ]

    composite = review_target_lifecycle_decision(
        session_factory,
        target_reference=target.sdbid,
        role="composite",
        state="system_only",
    )
    assert composite["interpretation"]["model_target"] is False
    assert "contributors" in composite["interpretation"]["multiplicity"]
