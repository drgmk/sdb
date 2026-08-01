from __future__ import annotations

from sqlalchemy import select

from sdb_identity.models.identity import ExternalIdentifier, Target
from sdb_identity.photometry.assignments import assign_measurement_target
from sdb_identity.review.dashboard import review_dashboard_report
from sdb_identity.review.actions import review_catalog_target_association_decision
from sdb_identity.samples.service import SampleService
from sdb_identity.identifiers import normalize_identifier
from sdb_identity.service import AddRequest, IdentityService
from tests.test_review_actions import _wise_measurements


def test_dashboard_lists_clean_unassigned_mixed_and_no_photometry_targets(
    session_factory,
):
    identity = IdentityService(session_factory)
    clean = identity.add(AddRequest(ra_deg=10.0, dec_deg=-20))
    unassigned = identity.add(AddRequest(ra_deg=10.0002, dec_deg=-20))
    mixed = identity.add(AddRequest(ra_deg=10.0004, dec_deg=-20))
    empty = identity.add(AddRequest(ra_deg=11.0, dec_deg=-20))
    ambiguity_target = identity.add(AddRequest(ra_deg=12.0, dec_deg=-20))
    exception_target = identity.add(AddRequest(ra_deg=13.0, dec_deg=-20))
    samples = SampleService(session_factory)
    samples.create("dashboard")
    for target in (clean, unassigned, mixed, empty):
        samples.add(
            "dashboard", target.sdbid, actor="test", reason="dashboard fixture",
        )
    with session_factory() as session:
        clean_id = session.scalar(
            select(Target.id).where(Target.sdbid == clean.sdbid)
        )
        session.add(ExternalIdentifier(
            target_id=clean_id,
            value="HD 123",
            normalized_value=normalize_identifier("HD 123"),
            source="submitted",
        ))
        session.commit()

    clean_measurements = _wise_measurements(
        session_factory, clean, source_id="clean-wise", ra=10.0,
    )
    for measurement in clean_measurements:
        assign_measurement_target(
            session_factory,
            measurement.id,
            clean.sdbid,
            role="contributor",
            method="fixture",
            actor="test",
            reason="clean ownership",
        )
    unassigned_measurements = _wise_measurements(
        session_factory, unassigned, source_id="unassigned-wise", ra=10.0002,
    )
    association_preview = review_catalog_target_association_decision(
        session_factory,
        target_reference=ambiguity_target.sdbid,
        detection_id=unassigned_measurements[0].detection_id,
        action="accept",
        reviewed_raw_row_id=unassigned_measurements[0].raw_row_id,
    )
    review_catalog_target_association_decision(
        session_factory,
        target_reference=ambiguity_target.sdbid,
        detection_id=unassigned_measurements[0].detection_id,
        action="accept",
        reviewed_raw_row_id=unassigned_measurements[0].raw_row_id,
        apply=True,
        actor="test",
        reason="fixture with two plausible source associations",
        expected_token=association_preview["state_token"],
    )
    mixed_measurements = _wise_measurements(
        session_factory, mixed, source_id="mixed-wise", ra=10.0004,
    )
    assign_measurement_target(
        session_factory,
        mixed_measurements[0].id,
        exception_target.sdbid,
        role="contributor",
        method="fixture",
        actor="test",
        reason="one band is an explicit attribution exception",
    )

    report = review_dashboard_report(session_factory, sample="dashboard")
    rows = {row["sdbid"]: row for row in report["rows"]}

    assert rows[clean.sdbid]["classification"] == "assigned_clean"
    assert rows[clean.sdbid]["display_name"] == "HD 123"
    assert rows[clean.sdbid]["priority"] == "none"
    assert (
        rows[unassigned.sdbid]["classification"]
        == "unassigned_excluded_photometry"
    )
    assert rows[unassigned.sdbid]["unassigned_detection_count"] == 1
    assert rows[mixed.sdbid]["classification"] == "mixed_band_ownership"
    assert rows[mixed.sdbid]["mixed_detection_count"] == 1
    assert rows[empty.sdbid]["classification"] == "no_current_photometry"
    assert report["summary"] == {
        "target_count": 4,
        "actionable_target_count": 3,
        "clean_target_count": 1,
        "scope_blocker_target_count": 0,
        "mixed_ownership_target_count": 1,
        "unassigned_target_count": 1,
        "no_photometry_target_count": 1,
        "detection_count": 3,
        "unassigned_detection_count": 1,
        "mixed_detection_count": 1,
    }
