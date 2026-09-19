from __future__ import annotations

from sqlalchemy import select

from sdb_identity.catalogs.acquisition import CatalogAcquisitionService
from sdb_identity.catalogs.associations import catalog_coverage_by_target
from sdb_identity.hierarchy.service import HierarchyService
from sdb_identity.models.catalogs import RawCatalogRow
from sdb_identity.models.identity import ExternalIdentifier, Target
from sdb_identity.photometry.assignments import assign_measurement_target
from sdb_identity.review.dashboard import review_dashboard_report
from sdb_identity.review.dashboard_cache import ReviewDashboardCache
from sdb_identity.review.actions import review_catalog_target_association_decision
from sdb_identity.review.sky_view import build_review_sky_view
from sdb_identity.samples.service import SampleService
from sdb_identity.samples.readiness import ReadinessService
from sdb_identity.identifiers import normalize_identifier
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.target_lifecycle import set_target_lifecycle
from tests.test_review_actions import _wise_measurements
from tests.test_catalog import FakeCatalog, candidate, measurement


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
        == "unassigned_photometry"
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
        "catalog_review_target_count": 0,
        "catalog_review_result_count": 0,
        "possible_duplicate_target_count": 0,
        "detection_count": 3,
        "unassigned_detection_count": 1,
        "mixed_detection_count": 1,
    }


def test_dashboard_all_targets_is_not_limited_to_sample_members(session_factory):
    identity = IdentityService(session_factory)
    sample_member = identity.add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    other_target = identity.add(AddRequest(ra_deg=20.0, dec_deg=-30.0))
    samples = SampleService(session_factory)
    samples.create("subset")
    samples.add(
        "subset",
        sample_member.sdbid,
        actor="test",
        reason="all-target review fixture",
    )

    sample_report = review_dashboard_report(session_factory, sample="subset")
    all_report = review_dashboard_report(session_factory, all_targets=True)

    assert [row["sdbid"] for row in sample_report["rows"]] == [
        sample_member.sdbid
    ]
    assert {row["sdbid"] for row in all_report["rows"]} == {
        sample_member.sdbid,
        other_target.sdbid,
    }
    assert all_report["selection"]["all"] is True


def test_dashboard_cache_refreshes_one_changed_target(session_factory):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10.0, dec_deg=-20.0)
    )
    cache = ReviewDashboardCache(
        session_factory,
        sample=None,
        all_targets=True,
        catalog_providers=None,
    )

    before = cache.get()
    assert before["rows"][0]["classification"] == "no_current_photometry"

    measurements = _wise_measurements(
        session_factory, target, source_id="cache-refresh-wise", ra=10.0,
    )
    cache.refresh_related(
        target_references=(target.sdbid,),
        detection_ids=(measurements[0].detection_id,),
    )
    after = cache.get()

    assert after is not before
    assert after["rows"][0]["classification"] == "assigned_clean"
    assert after["summary"]["clean_target_count"] == 1
    assert after["selection"]["selected_sdbids"] == [target.sdbid]


def test_dashboard_prioritizes_possible_duplicate_target(session_factory):
    identity = IdentityService(session_factory)
    existing = identity.add(AddRequest(ra_deg=10.0, dec_deg=20.0))
    from tests.fakes import FakeGaia, astrometry, gaia_candidate

    imported = IdentityService(
        session_factory,
        gaia=FakeGaia([gaia_candidate(
            "123",
            astrometry(10.00018, 20.0, epoch=2000.0, source="gaia_dr3"),
        )]),
    ).add(AddRequest(ra_deg=10.00018, dec_deg=20.0))
    samples = SampleService(session_factory)
    samples.create("duplicates")
    samples.add(
        "duplicates", imported.sdbid, actor="test", reason="review fixture",
    )

    report = review_dashboard_report(session_factory, sample="duplicates")
    row = report["rows"][0]

    assert row["classification"] == "possible_duplicate_target"
    assert row["priority"] == "highest"
    assert existing.sdbid in row["recommended_action"]
    assert row["possible_duplicates"][0]["other_sdbid"] == existing.sdbid
    assert report["summary"]["possible_duplicate_target_count"] == 1


def test_dashboard_makes_ambiguous_catalog_results_actionable(session_factory):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    samples = SampleService(session_factory)
    samples.create("catalog-review")
    samples.add(
        "catalog-review", target.sdbid,
        actor="test", reason="catalog review fixture",
    )
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("one", ra=10.00010),
            candidate("two", ra=10.00011),
        ]),
    }).refresh(target.target_id, "2mass")

    report = review_dashboard_report(
        session_factory,
        sample="catalog-review",
        catalog_providers=("2mass",),
    )
    row = report["rows"][0]

    assert row["classification"] == "catalog_association_review"
    assert row["priority"] == "high"
    assert row["recommended_action"] == (
        "review ambiguous catalog associations: 2mass"
    )
    assert row["catalog_review"] == [{
        "provider": "2mass",
        "status": "ambiguous",
        "error": None,
    }]
    assert row["providers"][0]["review_status"] == "ambiguous"
    assert report["summary"]["actionable_target_count"] == 1


def test_dashboard_drops_ambiguity_after_source_association_is_accepted(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    samples = SampleService(session_factory)
    samples.create("accepted-catalog-review")
    samples.add(
        "accepted-catalog-review", target.sdbid,
        actor="test", reason="catalog review fixture",
    )
    acquired = CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("one", ra=10.00010, measurements=[measurement()]),
            candidate(
                "two", ra=10.00011,
                measurements=[measurement(value=8.2)],
            ),
        ]),
    }).refresh(target.target_id, "2mass")
    with session_factory() as session:
        chosen = session.scalar(
            select(RawCatalogRow).where(
                RawCatalogRow.run_id == acquired.run_id,
                RawCatalogRow.source_id == "two",
            )
        )
    preview = review_catalog_target_association_decision(
        session_factory,
        target_reference=target.sdbid,
        detection_id=chosen.detection_id,
        action="accept",
        reviewed_raw_row_id=chosen.id,
    )
    review_catalog_target_association_decision(
        session_factory,
        target_reference=target.sdbid,
        detection_id=chosen.detection_id,
        action="accept",
        reviewed_raw_row_id=chosen.id,
        apply=True,
        actor="test",
        reason="candidate accepted from the ordinary source controls",
        expected_token=preview["state_token"],
    )

    report = review_dashboard_report(
        session_factory,
        sample="accepted-catalog-review",
        catalog_providers=("2mass",),
    )
    row = report["rows"][0]

    assert row["catalog_review"] == []
    assert row["providers"][0]["review_status"] is None
    assert row["classification"] != "catalog_association_review"
    assert report["summary"]["catalog_review_target_count"] == 0


def test_dashboard_resolves_composite_ambiguity_through_physical_members(
    session_factory,
):
    identity = IdentityService(session_factory)
    system = identity.add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    component_a = identity.add(AddRequest(
        ra_deg=9.9997, dec_deg=-20.0,
    ))
    component_b = identity.add(AddRequest(
        ra_deg=10.0003, dec_deg=-20.0,
    ))
    hierarchy = HierarchyService(session_factory)
    hierarchy.create_system("resolved AB", primary=system.sdbid)
    hierarchy.add_member(
        "resolved AB", component_a.sdbid, component_label="A",
    )
    hierarchy.add_member(
        "resolved AB", component_b.sdbid, component_label="B",
    )
    set_target_lifecycle(
        session_factory, system.sdbid,
        role="composite", state="system_only", actor="test",
        reason="composite review fixture",
    )
    for component, label in ((component_a, "A"), (component_b, "B")):
        set_target_lifecycle(
            session_factory, component.sdbid,
            role="physical", state="active", actor="test",
            reason=f"physical component {label}",
        )
    samples = SampleService(session_factory)
    samples.create("component-resolution")
    samples.add(
        "component-resolution", system.sdbid,
        actor="test", reason="component resolution fixture",
    )

    candidates = [
        candidate(
            "component-a", ra=9.9997, dec=-20.0,
            measurements=[measurement(value=7.0)],
        ),
        candidate(
            "component-b", ra=10.0003, dec=-20.0,
            measurements=[measurement(value=8.0)],
        ),
    ]
    service = CatalogAcquisitionService(
        session_factory, {"2mass": FakeCatalog(candidates)},
    )
    assert service.refresh(system.sdbid, "2mass").status == "ambiguous"
    assert service.refresh(component_a.sdbid, "2mass").status == "match"

    partial = review_dashboard_report(
        session_factory,
        sample="component-resolution",
        catalog_providers=("2mass",),
    )["rows"][0]
    assert partial["classification"] == "catalog_association_review"

    assert service.refresh(component_b.sdbid, "2mass").status == "match"
    resolved = review_dashboard_report(
        session_factory,
        sample="component-resolution",
        catalog_providers=("2mass",),
    )["rows"][0]
    assert resolved["catalog_review"] == []
    assert resolved["classification"] != "catalog_association_review"
    readiness = ReadinessService(session_factory).report(
        "component-resolution", providers=("2mass",),
    )
    assert not any(
        issue["kind"] == "provider_result"
        for issue in readiness.issues
    )

    with session_factory() as session:
        coverage = catalog_coverage_by_target(
            session, [system.target_id], providers=("2mass",),
        )[0]
    assert coverage["current_status_by_provider"] == {
        "2mass": "resolved_by_components",
    }
    associations = coverage["component_resolutions_by_provider"][
        "2mass"
    ]["associations"]
    assert {
        (row["source_id"], row["targets"][0]["component_labels"][0])
        for row in associations
    } == {("component-a", "A"), ("component-b", "B")}

    view = build_review_sky_view(session_factory, system.sdbid)
    points = {
        point.source_id: point
        for point in view.points
        if point.provider == "2mass"
        and point.source_id in {"component-a", "component-b"}
    }
    assert set(points) == {"component-a", "component-b"}
    assert {point.status for point in points.values()} == {"associated"}
    assert all(point.accepted for point in points.values())
    assert points["component-a"].linked_target_sdbids == (
        component_a.sdbid,
    )
    assert points["component-b"].linked_target_sdbids == (
        component_b.sdbid,
    )
