from __future__ import annotations

from sdb_identity.photometry import assign_measurement_target
from sdb_identity.review_dashboard import review_dashboard_report
from sdb_identity.samples import SampleService
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
    samples = SampleService(session_factory)
    samples.create("dashboard")
    for target in (clean, unassigned, mixed, empty):
        samples.add(
            "dashboard", target.sdbid, actor="test", reason="dashboard fixture",
        )

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
    _wise_measurements(
        session_factory, unassigned, source_id="unassigned-wise", ra=10.0002,
    )
    mixed_measurements = _wise_measurements(
        session_factory, mixed, source_id="mixed-wise", ra=10.0004,
    )
    assign_measurement_target(
        session_factory,
        mixed_measurements[0].id,
        mixed.sdbid,
        role="contributor",
        method="fixture",
        actor="test",
        reason="one band only",
    )

    report = review_dashboard_report(session_factory, sample="dashboard")
    rows = {row["sdbid"]: row for row in report["rows"]}

    assert rows[clean.sdbid]["classification"] == "assigned_clean"
    assert rows[clean.sdbid]["priority"] == "none"
    assert rows[unassigned.sdbid]["classification"] == "unassigned_photometry"
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
