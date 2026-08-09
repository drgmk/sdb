from __future__ import annotations

import json
import math

import pytest

from sdb_identity.catalogs.policy import catalog_source_display_name
from sdb_identity.catalogs.acquisition import CatalogAcquisitionService
from sdb_identity.catalogs.types import (
    CatalogAttributeValue,
    CatalogCandidate,
    MeasurementValue,
)
from sdb_identity.hierarchy.service import HierarchyService
from sdb_identity.models.identity import (
    AstrometricSolution,
    ExternalIdentifier,
    MatchCandidate,
    Submission,
)
from sdb_identity.models.metadata import MetadataRun, SimbadMetadata
from sdb_identity.review.sky_view import (
    ReviewSkyView,
    SkyArrow,
    SkyPoint,
    _deduplicate_points,
    _catalog_component_summary,
    _midpoint_position,
    _offset_position,
    build_review_sky_view,
)
from sdb_identity.review.sky_render import (
    _uncertainty_ellipse_xy,
    render_review_sky_html,
)
from sdb_identity.identifiers import normalize_identifier
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement
from tests.fakes import FakeGaia, FakeSimbad, astrometry, gaia_candidate, simbad_result


def test_catalog_source_display_names_are_adapter_owned():
    assert catalog_source_display_name("2mass", "23123456-1234567") == "2MASS J23123456-1234567"
    assert catalog_source_display_name("allwise", "J231234.56-123456.7") == "AllWISE J231234.56-123456.7"
    assert catalog_source_display_name("gaia_dr3", "123456789") == "Gaia DR3 123456789"
    assert catalog_source_display_name("tycho2", "1234-567-1") == "TYC 1234-567-1"
    assert catalog_source_display_name(
        "hip2", "36948", {"HIP": 36948}
    ) == "HIP 36948"
    assert catalog_source_display_name(
        "paunzen15",
        "7109|TYC2=2638|TYC3=1",
        {"TYC1": 7109, "TYC2": 2638, "TYC3": 1},
    ) == "TYC 7109-2638-1"
    assert catalog_source_display_name(
        "v70a",
        "GJ 1294|Comp=B",
        {"Name": "GJ 1294", "Comp": "B"},
    ) == "GJ 1294 B"


def test_v70a_component_is_surfaced_in_catalog_point_details():
    assert _catalog_component_summary(
        "v70a",
        {"Name": "GJ 1294", "Comp": "B"},
        "GJ 1294|Comp=B",
    ) == "B — V/70A component B"


def test_review_sky_view_includes_identity_catalog_points_but_hides_no_match_points(session_factory):
    identity_gaia = FakeGaia([
        gaia_candidate("gaia-a", astrometry(10.00001, -20, epoch=2016, source="gaia_dr3")),
        gaia_candidate("gaia-b", astrometry(10.00002, -20, epoch=2016, source="gaia_dr3")),
    ])
    target = IdentityService(session_factory, gaia=identity_gaia).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("2mass-a", ra=10.00010, dec=-20, measurements=[measurement()]),
            candidate("2mass-b", ra=10.00011, dec=-20, measurements=[measurement()]),
        ]),
        "empty": FakeCatalog([], name="empty", release="fake-empty"),
    }).refresh(target.sdbid, "2mass")
    CatalogAcquisitionService(session_factory, {
        "empty": FakeCatalog([], name="empty", release="fake-empty"),
    }).refresh(target.sdbid, "empty")

    view = build_review_sky_view(session_factory, target.sdbid)

    values = {(point.kind, point.provider, point.status, point.source_id) for point in view.points}
    assert ("target", "sdb", "target", target.sdbid) in values
    assert ("identity", "gaia_dr3", "candidate", "gaia-a") in values
    assert ("identity", "gaia_dr3", "candidate", "gaia-b") in values
    assert (
        "catalog_association", "2mass", "ambiguous", "2mass-a"
    ) in values
    assert (
        "catalog_association", "2mass", "ambiguous", "2mass-b"
    ) in values
    assert not any(
        point.kind == "catalog" and point.provider == "empty"
        for point in view.points
    )


def test_review_sky_view_renders_rotated_iras_position_uncertainty(session_factory):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10.0, dec_deg=-20.0)
    )
    iras = CatalogCandidate(
        source_id="00001-2000",
        ra_deg=10.0,
        dec_deg=-20.0,
        epoch=1983.5,
        payload={
            "IRAS": "00001-2000",
            "Major": 16,
            "Minor": 4,
            "PosAng": 35,
        },
    )
    CatalogAcquisitionService(session_factory, {
        "iras_psc": FakeCatalog(
            [iras], name="iras_psc", release="fake-iras-psc"
        ),
    }).refresh(target.sdbid, "iras_psc")

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.provider == "iras_psc")

    assert point.uncertainty_major_arcsec == 16.0
    assert point.uncertainty_minor_arcsec == 4.0
    assert point.uncertainty_position_angle_deg == 35.0

    html = render_review_sky_html(view)
    assert "position uncertainty: 16.00\\u2033 \\u00d7 4.00\\u2033" in html
    assert "PA 35.00\\u00b0 east of north" in html


def test_position_uncertainty_angle_is_measured_east_of_north():
    x_values, y_values = _uncertainty_ellipse_xy(
        0.0, 0.0, 16.0, 4.0, 0.0, samples=4,
    )

    assert x_values[:2] == pytest.approx([0.0, 4.0], abs=1e-12)
    assert y_values[:2] == pytest.approx([16.0, 0.0], abs=1e-12)


def test_review_sky_view_projects_reconciled_catalog_candidate_from_nearby_target(
    session_factory,
):
    identity = IdentityService(session_factory)
    catalog_target = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    component_target = identity.add(
        AddRequest(ra_deg=10.0 + 4.2 / 3600.0, dec_deg=0.0)
    )
    result = CatalogAcquisitionService(session_factory, {
        "tycho2": FakeCatalog(
            [candidate("9134-1714-2", ra=10.0 + 4.1 / 3600.0, dec=0.0)],
            name="tycho2",
            release="fake-tycho2",
        ),
    }).refresh(catalog_target.sdbid, "tycho2")
    assert result.status == "ambiguous"

    view = build_review_sky_view(session_factory, component_target.sdbid)
    point = next(
        point for point in view.points
        if point.provider == "tycho2" and point.source_id == "9134-1714-2"
    )
    html = render_review_sky_html(view)

    assert point.kind == "catalog_association"
    assert point.status == "candidate"
    assert point.target_id == component_target.target_id
    assert point.run_target_sdbid == catalog_target.sdbid
    assert point.linked_target_sdbids == (component_target.sdbid,)
    assert point.separation_arcsec == pytest.approx(0.1, abs=0.01)
    assert "catalog detection reconciles to" in point.note
    assert f"encountered by {catalog_target.sdbid}" in point.note
    assert "catalog query for" in html


def test_review_sky_view_marks_identity_candidate_linked_to_sibling_target(session_factory):
    identity = IdentityService(session_factory)
    primary = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0))
    secondary = identity.add(AddRequest(ra_deg=10.0019444444, dec_deg=0.0))
    with session_factory.begin() as session:
        session.add(ExternalIdentifier(
            target_id=secondary.target_id,
            value="Gaia DR3 123456789",
            normalized_value=normalize_identifier("Gaia DR3 123456789"),
            source="simbad",
        ))
        submission = Submission(target_id=primary.target_id, status="created")
        session.add(submission)
        session.flush()
        session.add(MatchCandidate(
            submission_id=submission.id,
            provider="gaia_dr3",
            source_id="123456789",
            ra_deg=10.0019444444,
            dec_deg=0.0,
            epoch=2016.0,
            proper_motion_available=False,
            separation_arcsec=7.0,
            score=0.01,
            score_details="{}",
        ))

    view = build_review_sky_view(session_factory, primary.sdbid)
    point = next(
        point for point in view.points
        if point.kind == "identity" and point.source_id == "123456789"
    )
    html = render_review_sky_html(view)

    assert point.linked_target_sdbids == (secondary.sdbid,)
    assert "nearby SDB target" in point.cross_candidate_reason
    assert '"linked_target_sdbids"' in html
    assert secondary.sdbid in html


def test_review_simbad_metadata_point_uses_canonical_simbad_pm(session_factory):
    simbad_astrometry = astrometry(
        10,
        -20,
        pmra=1450.34,
        pmdec=-19.38,
        source="simbad",
    )
    target = IdentityService(
        session_factory,
        simbad=FakeSimbad({"HD 3443": simbad_result("HD   3443", simbad_astrometry)}),
    ).add(AddRequest(name="HD 3443"))
    with session_factory() as session:
        run = MetadataRun(
            target_id=target.target_id,
            provider="simbad",
            release="fake-simbad",
            status="match",
            is_current=True,
            query_identifier="HD 3443",
            candidate_count=1,
        )
        session.add(run)
        session.flush()
        session.add(
            SimbadMetadata(
                run_id=run.id,
                target_id=target.target_id,
                oid=123,
                main_id="HD   3443",
                ra_deg=10,
                dec_deg=-20,
            )
        )
        session.commit()

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.kind == "metadata" and point.provider == "simbad")

    assert point.pm_ra_cosdec_masyr == 1450.34
    assert point.pm_dec_masyr == -19.38
    assert point.pm_source == "canonical simbad astrometry"


def test_review_simbad_metadata_point_uses_metadata_pm_before_canonical_pm(session_factory):
    gaia = FakeGaia([
        gaia_candidate("gaia", astrometry(10, -20, epoch=2016, source="gaia_dr3")),
    ])
    target = IdentityService(session_factory, gaia=gaia).add(AddRequest(ra_deg=10, dec_deg=-20))
    with session_factory() as session:
        run = MetadataRun(
            target_id=target.target_id,
            provider="simbad",
            release="fake-simbad",
            status="match",
            is_current=True,
            query_identifier="HD 2475",
            candidate_count=1,
        )
        session.add(run)
        session.flush()
        session.add(
            SimbadMetadata(
                run_id=run.id,
                target_id=target.target_id,
                oid=123,
                main_id="HD   2475",
                ra_deg=10,
                dec_deg=-20,
                pm_ra_cosdec_masyr=123.4,
                pm_dec_masyr=-56.7,
                proper_motion_bibcode="2021A&A...000....6P",
            )
        )
        session.commit()

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.kind == "metadata" and point.provider == "simbad")

    assert point.pm_ra_cosdec_masyr == 123.4
    assert point.pm_dec_masyr == -56.7
    assert point.pm_source == "2021A&A...000....6P"


def test_review_sky_view_uses_simbad_metadata_pm_when_canonical_pm_is_missing(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    with session_factory() as session:
        run = MetadataRun(
            target_id=target.target_id,
            provider="simbad",
            release="fake-simbad",
            status="match",
            is_current=True,
            query_identifier="Barnard's star",
            candidate_count=1,
        )
        session.add(run)
        session.flush()
        session.add(
            SimbadMetadata(
                run_id=run.id,
                target_id=target.target_id,
                oid=123,
                main_id="NAME Barnard's star",
                ra_deg=10,
                dec_deg=-20,
                pm_ra_cosdec_masyr=-801.551,
                pm_dec_masyr=10362.394,
                proper_motion_bibcode="2020yCat.1350....0G",
            )
        )
        session.commit()
    CatalogAcquisitionService(session_factory, {
        "old": FakeCatalog([
            CatalogCandidate(
                source_id="old-a",
                ra_deg=10.0,
                dec_deg=-20.0,
                epoch=1990.0,
                payload={"id": "old-a"},
                measurements=(measurement(),),
            ),
        ], name="old", release="fake-old"),
    }).refresh(target.sdbid, "old")

    view = build_review_sky_view(session_factory, target.sdbid)
    arrow = next(arrow for arrow in view.arrows if arrow.target_id == target.target_id)
    catalog_point = next(
        point for point in view.points
        if point.provider == "old" and point.source_id == "old-a"
    )

    assert arrow.provider == "simbad metadata"
    assert arrow.source_id == "NAME Barnard's star"
    assert arrow.pm_ra_cosdec_masyr == -801.551
    assert arrow.pm_dec_masyr == 10362.394
    assert catalog_point.ra_deg != 10.0
    assert catalog_point.pm_source == "assumed target PM (simbad metadata)"
    assert "using target PM as counterpart hypothesis" in catalog_point.note

    html = render_review_sky_html(view)
    assert '"name":"catalog to 2000"' in html
    assert '"name":"PM"' in html


def test_review_sky_html_has_annotation_toggle_and_embedded_data(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    view = build_review_sky_view(session_factory, target.sdbid)

    html = render_review_sky_html(view)

    assert "toggle-annotations" in html
    assert "Show target labels" in html
    assert "let annotationsVisible = false" in html
    assert 'id="toggle-positional-uncertainties"' in html
    assert "Hide positional uncertainties" in html
    assert "Plotly.newPlot" in html
    assert "{{responsive" not in html
    assert '"responsive": false' in html
    assert '"scaleanchor":"y"' in html
    assert '"scaleratio":1' in html
    assert "__CENTER_RA__" not in html
    assert '<script src="https://cdn.plot.ly' not in html
    assert target.sdbid in html
    assert '"points"' in html


def test_review_sky_html_can_show_selected_photometry_beams(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogAcquisitionService(session_factory, {
        "beamcat": FakeCatalog([
            candidate(
                "beam-source",
                measurements=[
                    MeasurementValue(
                        band="W1",
                        value=7.1,
                        error=0.02,
                        unit="mag",
                        bibcode="fake",
                        resolution_major_arcsec=6.1,
                        resolution_minor_arcsec=6.1,
                        resolution_kind="psf_fwhm",
                        resolution_reference="test reference",
                    ),
                    MeasurementValue(
                        band="W4",
                        value=6.2,
                        error=0.05,
                        unit="mag",
                        bibcode="fake",
                        resolution_major_arcsec=12.0,
                        resolution_minor_arcsec=12.0,
                        resolution_kind="psf_fwhm",
                        resolution_reference="test reference",
                    ),
                ],
            ),
        ], name="beamcat", release="fake-beamcat"),
    }).refresh(target.sdbid, "beamcat")

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.provider == "beamcat")

    assert [beam.band for beam in point.photometry_beams] == ["W1", "W4"]
    assert point.photometry_beams[0].major_arcsec == 6.1

    html = render_review_sky_html(view)

    assert "toggle-beams" in html
    assert "Show photometry beams" in html
    assert "photometry_beams" in html
    assert '"review_kind":"beam"' in html
    assert "psf_fwhm: 6.10" in html
    assert "psf_fwhm: 12.00" in html
    assert "full width" in html


def test_review_sky_html_renders_pm_vectors_as_plotly_trace():
    view = ReviewSkyView(
        target_id=1,
        sdbid="sdbid-v3-004000.00-200000.0",
        center_ra_deg=10.0,
        center_dec_deg=-20.0,
        radius_arcsec=5.0,
        points=(
            SkyPoint(
                kind="target",
                provider="sdb",
                status="target",
                source_id="sdbid-v3-004000.00-200000.0",
                ra_deg=10.0,
                dec_deg=-20.0,
                separation_arcsec=0.0,
                accepted=True,
            ),
        ),
        arrows=(
            SkyArrow(
                kind="proper_motion",
                provider="gaia_dr3",
                source_id="moving",
                ra_deg=10.0,
                dec_deg=-20.0,
                pm_ra_cosdec_masyr=1000.0,
                pm_dec_masyr=-500.0,
                years=10.0,
            ),
        ),
    )

    html = render_review_sky_html(view)

    assert '"name":"PM"' in html
    assert "pmRA*=" in html
    assert "circle" in html
    assert "arrowAnnotations" not in html
    assert "grid-template-columns: minmax(0, 2fr) minmax(0, 6fr) minmax(0, 3fr) minmax(0, 3.5fr)" in html
    assert "max-width: 2100px" in html
    assert "resizeSkySquare" in html
    assert "ResizeObserver" in html
    assert "plot._fullLayout" in html
    assert "aspect-ratio: 1 / 1" in html
    assert 'class="sky-frame"' in html
    assert ".sky-frame { box-sizing: border-box; width: 100%; aspect-ratio: 1 / 1; min-height: 0; background: var(--panel); border: 1px solid var(--grid); border-radius: 8px; }" in html
    assert "layoutHeight" in html
    assert 'id="component-context"' not in html
    assert "Catalog hierarchy &amp; components" in html
    assert html.index("<h2>Current target</h2>") < html.index(
        "<h2>System context</h2>"
    )
    assert 'class="external-actions"' not in html
    assert 'id="current-target-simbad"' not in html
    assert '!target.is_requested_target && !relativeTargetIds.has(target.sdbid)' in html
    assert "targetReviewLink" in html
    assert "targetMainId" in html
    assert "firstUsefulIdentifier" not in html
    assert "simbad_main_id_by_target" in html
    assert "simbad_metadata_by_target" in html
    assert html.index("<h3>Immediate SIMBAD relatives</h3>") < html.index(
        "<h3>Other nearby SDB targets</h3>"
    )
    assert 'stellar_or_substellar_component: "stellar"' in html
    assert 'planetary_or_disk: "planet"' in html
    assert "d = ${distance}" in html
    assert "compactBands" not in html
    assert "candidateCounts" not in html
    assert 'id="photometry-context"' in html
    assert 'class="plot-column"' in html
    assert 'class="details-columns"' in html
    assert "applySelection" in html
    assert "selectedPointIndex" in html
    assert 'id="toggle-point-list"' in html
    assert "pointIsDefaultRelevant" in html
    assert "explicitSystemMembers" in html
    assert "Show all plotted items (" in html
    assert 'point.provider === "sdb"' in html
    assert 'class="point-primary"' in html
    assert 'class="point-secondary"' in html
    assert 'String(point.status).replaceAll("_", " ")' in html
    assert 'point.status === "accepted"' in html
    assert 'point.accepted && point.status !== "rejected"' in html
    assert "separation_arcsec - b.separation_arcsec" in html
    assert 'match: "#f59e0b"' in html
    assert 'accepted: "#16a34a"' in html
    assert 'rejected: "#2563eb"' in html
    assert 'review_neighbour: "#94a3b8"' in html


def test_review_sky_view_deduplicates_same_provider_source_position_for_display():
    first = SkyPoint(
        kind="identity",
        provider="gaia_dr3",
        status="accepted",
        source_id="123",
        ra_deg=10.0,
        dec_deg=-20.0,
        separation_arcsec=0.0,
        accepted=True,
        candidate_id=1,
        note="identity row",
    )
    second = SkyPoint(
        kind="catalog",
        provider="gaia_dr3",
        status="accepted",
        source_id="123",
        ra_deg=10.0,
        dec_deg=-20.0,
        separation_arcsec=0.0,
        accepted=True,
        raw_row_id=2,
        provenance=({
            "table_id": "I/355/gaiadr3",
            "access_url": (
                "https://vizier.cds.unistra.fr/viz-bin/VizieR-5?"
                "-out.add=.&-source=I%2F355%2Fgaiadr3&Source===123"
            ),
        },),
        note="catalog row",
    )

    points = _deduplicate_points([first, second])

    assert len(points) == 1
    assert points[0].kind == "identity+catalog"
    assert points[0].candidate_id == 1
    assert points[0].raw_row_id == 2
    assert points[0].provenance == second.provenance


def test_review_sky_view_includes_nearby_targets_and_proper_motion_arrows(session_factory):
    moving_gaia = FakeGaia([
        gaia_candidate(
            "moving",
            astrometry(10, -20, epoch=2016, pmra=1000, pmdec=-500, source="gaia_dr3"),
        ),
    ])
    target = IdentityService(session_factory, gaia=moving_gaia).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    initial_view = build_review_sky_view(session_factory, target.sdbid, radius_arcsec=5)
    nearby = IdentityService(session_factory).add(
        AddRequest(
            ra_deg=initial_view.center_ra_deg + 0.00060,
            dec_deg=initial_view.center_dec_deg,
            epoch=2000,
        )
    )
    outside_small_radius = IdentityService(session_factory).add(
        AddRequest(
            ra_deg=initial_view.center_ra_deg + 12.0 / (
                3600.0 * math.cos(math.radians(initial_view.center_dec_deg))
            ),
            dec_deg=initial_view.center_dec_deg,
            epoch=2000,
        )
    )

    view = build_review_sky_view(session_factory, target.sdbid, radius_arcsec=5)

    assert view.radius_arcsec == 5
    assert view.system_context["radius_arcsec"] == 5
    assert any(
        point.kind == "nearby_target" and point.source_id == nearby.sdbid
        for point in view.points
    )
    assert not any(
        point.kind == "nearby_target"
        and point.source_id == outside_small_radius.sdbid
        for point in view.points
    )
    assert outside_small_radius.sdbid not in {
        row["sdbid"] for row in view.system_context["nearby_sdb_targets"]
    }
    assert any(
        arrow.kind == "proper_motion"
        and arrow.source_id == "moving"
        and arrow.pm_ra_cosdec_masyr == 1000
        and arrow.pm_dec_masyr == -500
        and arrow.years == 10.0
        for arrow in view.arrows
    )

    wider_view = build_review_sky_view(
        session_factory,
        target.sdbid,
        radius_arcsec=20,
    )
    assert wider_view.system_context["radius_arcsec"] == 20
    assert any(
        point.kind == "nearby_target"
        and point.source_id == outside_small_radius.sdbid
        for point in wider_view.points
    )
    assert outside_small_radius.sdbid in {
        row["sdbid"] for row in wider_view.system_context["nearby_sdb_targets"]
    }


def test_review_sky_view_auto_radius_includes_explicit_system_members_and_caps_at_ten_arcmin(
    session_factory,
):
    identity = IdentityService(session_factory)
    target = identity.add(AddRequest(ra_deg=10.0, dec_deg=0.0, epoch=2000.0))
    ordinary_neighbour = identity.add(AddRequest(
        ra_deg=10.0 + 50.0 / 3600.0,
        dec_deg=0.0,
        epoch=2000.0,
    ))
    system_member = identity.add(AddRequest(
        ra_deg=10.0 + 120.0 / 3600.0,
        dec_deg=0.0,
        epoch=2000.0,
    ))
    capped_member = identity.add(AddRequest(
        ra_deg=10.0 + 700.0 / 3600.0,
        dec_deg=0.0,
        epoch=2000.0,
    ))
    hierarchy = HierarchyService(session_factory)
    system = hierarchy.create_system("wide test system", primary=target.sdbid)
    hierarchy.add_member(system.name, system_member.sdbid, component_label="B")
    hierarchy.add_member(system.name, capped_member.sdbid, component_label="C")

    view = build_review_sky_view(session_factory, target.sdbid)
    plotted_nearby = {
        point.source_id for point in view.points if point.kind == "nearby_target"
    }

    assert view.radius_arcsec == 600.0
    assert view.system_context["radius_arcsec"] == 600.0
    assert ordinary_neighbour.sdbid in plotted_nearby
    assert system_member.sdbid in plotted_nearby
    assert capped_member.sdbid not in plotted_nearby
    assert capped_member.sdbid in view.system_context["system_memberships_by_target"]


def test_review_sky_view_displays_catalog_positions_at_epoch_2000_when_target_pm_available(session_factory):
    moving_gaia = FakeGaia([
        gaia_candidate(
            "moving",
            astrometry(10, -20, epoch=2016, pmra=1000, pmdec=0, source="gaia_dr3"),
        ),
    ])
    target = IdentityService(session_factory, gaia=moving_gaia).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    CatalogAcquisitionService(session_factory, {
        "old": FakeCatalog([
            CatalogCandidate(
                source_id="old-a",
                ra_deg=10.0,
                dec_deg=-20.0,
                epoch=1990.0,
                payload={"id": "old-a"},
                measurements=(measurement(),),
            ),
        ], name="old", release="fake-old"),
    }).refresh(target.sdbid, "old")

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.provider == "old" and point.source_id == "old-a")

    assert point.native_epoch == 1990.0
    assert point.display_epoch == 2000.0
    assert "using target PM as counterpart hypothesis" in point.note
    assert point.ra_deg != 10.0
    assert point.pm_source == "assumed target PM (gaia_dr3)"

    html = render_review_sky_html(view)
    assert '"name":"catalog to 2000"' in html
    assert "proper_motion" in html


def test_review_sky_view_includes_photometry_for_accepted_catalog_row(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            CatalogCandidate(
                source_id="2mass-a",
                ra_deg=10.0,
                dec_deg=-20.0,
                epoch=1999.3,
                payload={
                    "2MASS": "2mass-a", "RAJ2000": 10.0,
                    "DEJ2000": -20.0, "prox": 1.7, "_r": 0.1,
                },
                measurements=(measurement(),),
            ),
        ]),
    }).refresh(target.sdbid, "2mass")

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.provider == "2mass" and point.source_id == "2mass-a")

    assert any(value.startswith("2MJ=") for value in point.photometry)
    assert "nearest catalog source (prox)=1.70 arcsec" in point.attributes
    assert not any("(_r)" in value for value in point.attributes)
    assert view.system_context is not None
    assert "measurement_assignment_matrix" in view.system_context
    html = render_review_sky_html(view)
    assert "photometry" in html
    assert "nearest catalog source" in html
    assert '"source_display_name": "2MASS J2mass-a"' in html
    assert "<th>detection</th>" in html
    assert "row.band_count" in html
    assert "mixed assignments" in html


def test_review_sky_view_uses_snapshot_catalog_identifier_as_display_id(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    hp = MeasurementValue(
        band="HP",
        value=8.36,
        resolution_major_arcsec=0.1,
        resolution_minor_arcsec=0.1,
        resolution_kind="test",
        resolution_reference="test",
    )
    CatalogAcquisitionService(session_factory, {
        "hip2": FakeCatalog(
            [CatalogCandidate(
                source_id="36948",
                ra_deg=10.0,
                dec_deg=-20.0,
                epoch=1991.25,
                payload={"HIP": 36948},
                measurements=(hp,),
            )],
            name="hip2",
            release="fake-hip2",
            query_epoch=1991.25,
        ),
    }).refresh(target.sdbid, "hip2")

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.provider == "hip2")

    assert point.source_id == "36948"
    assert point.source_display_name == "HIP 36948"
    matrix = view.system_context["measurement_assignment_matrix"]
    assert matrix["rows"][0]["source_display_name"] == "HIP 36948"
    html = render_review_sky_html(view)
    assert '"source_display_name": "HIP 36948"' in html
    assert '[["ID", sourceLink(pointDisplayId(point), point.provenance), true]' in html



def test_review_sky_view_marks_catalog_review_neighbour_as_muted_context(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    CatalogAcquisitionService(session_factory, {
        "allwise": FakeCatalog([
            CatalogCandidate(
                source_id="wise-neighbour",
                ra_deg=10.0024,
                dec_deg=-20.0,
                epoch=2010.5,
                payload={
                    "AllWISE": "wise-neighbour",
                    "RAJ2000": 10.0024,
                    "DEJ2000": -20.0,
                    "nb": 2,
                    "prox": 1.1,
                    "_sdb_association": {
                        "review_only": True,
                        "candidate_separation_arcsec": 8.123456789,
                        "acceptance_radius_arcsec": 2.0,
                        "query_radius_arcsec": 15.0,
                    },
                },
                measurements=(measurement(),),
            ),
        ], name="allwise", release="fake-allwise"),
    }).refresh(target.sdbid, "allwise")

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.provider == "allwise")
    html = render_review_sky_html(view)

    assert point.kind == "catalog_association"
    assert point.status == "review_neighbour"
    assert point.accepted is False
    assert "review-only catalogue neighbour" in point.attributes
    assert "candidate separation=8.12 arcsec" in point.attributes
    assert "acceptance radius=2.00 arcsec" in point.attributes
    assert "query radius=15.00 arcsec" in point.attributes
    assert "simultaneous PSF components (nb)=2.00" in point.attributes
    assert not any("(prox)" in value for value in point.attributes)
    assert "review_neighbour" in html
    assert "#94a3b8" in html
    assert "circle-open" in html


def test_allwise_apparent_motion_is_not_used_as_proper_motion(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    with session_factory.begin() as session:
        solution = session.query(AstrometricSolution).one()
        solution.pm_ra_cosdec_masyr = 120.0
        solution.pm_dec_masyr = -45.0
        solution.proper_motion_available = True
    CatalogAcquisitionService(session_factory, {
        "allwise": FakeCatalog([
            CatalogCandidate(
                source_id="WISEA test",
                ra_deg=10.0,
                dec_deg=-20.0,
                epoch=2010.5,
                payload={},
                measurements=(measurement(),),
                attributes=(
                    CatalogAttributeValue(
                        key="apparent_motion_ra_cosdec", value_float=900.0,
                        unit="mas/yr", reference="AllWISE apparent-motion fit",
                    ),
                    CatalogAttributeValue(
                        key="apparent_motion_dec", value_float=-700.0,
                        unit="mas/yr", reference="AllWISE apparent-motion fit",
                    ),
                    # Legacy databases used generic PM keys for the same
                    # apparent-motion fit. The review reader must ignore them
                    # even before the provider is refreshed.
                    CatalogAttributeValue(
                        key="pm_ra_cosdec", value_float=900.0,
                        unit="mas/yr", reference="legacy AllWISE apparent motion",
                    ),
                    CatalogAttributeValue(
                        key="pm_dec", value_float=-700.0,
                        unit="mas/yr", reference="legacy AllWISE apparent motion",
                    ),
                ),
            ),
        ], name="allwise", release="fake-allwise", query_epoch=2010.5),
    }).refresh(target.sdbid, "allwise")

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.provider == "allwise")

    assert point.pm_ra_cosdec_masyr == 120.0
    assert point.pm_dec_masyr == -45.0
    assert point.pm_source == "assumed target PM (input)"
    assert "using target PM as counterpart hypothesis" in point.note
    assert any(value.startswith("apparent_motion_ra_cosdec=") for value in point.attributes)
    assert all("AllWISE apparent-motion fit" not in value for value in (point.pm_source,))

def test_review_sky_view_includes_hierarchy_candidates_and_geometry(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,2024,90,3.0,7.1,8.2\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=10)

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.kind == "hierarchy")

    assert point.provider == "wds"
    assert point.status == "candidate"
    assert point.candidate_id is not None
    assert any(value.startswith("rho=3") for value in point.attributes)
    assert any(value.startswith("pa=90") for value in point.attributes)
    assert len(view.segments) == 1
    assert view.segments[0].provider == "wds"
    assert view.segments[0].candidate_id == point.candidate_id
    assert view.segments[0].native_id == "00057+4549"
    assert view.segments[0].reference_label == "A"
    assert view.segments[0].component_label == "B"
    assert view.segments[0].relation_type == "group"

    html = render_review_sky_html(view)
    assert '"name":"hierarchy"' in html
    assert '"relation_type":"group"' in html
    assert "hierarchy-tree" in html
    assert (
        "https://vizier.cds.unistra.fr/viz-bin/VizieR-5?"
        "-out.add=.&-source=B%2Fwds%2Fwds&WDS===00057%2B4549"
    ) in html
    assert "Hierarchy" in html
    assert "System context" in html
    assert '"hierarchy_tree"' in html
    assert '"system_context"' in html
    assert "System photometry matrix" in html
    assert "assignment-matrix" in html
    assert "matrix-detection needs-review" in html
    assert "--review-cell: #fff1f2" in html
    assert 'id="toggle-review-drawer"' in html
    assert "Show review tools" in html
    assert "Hide review tools" in html
    assert "sdb-review-drawer-toggle" in html
    assert "ⓘ" in html
    assert "Measurement assignment proposals</h3>" not in html
    assert "STF3050" in html


def test_review_sky_view_uses_persisted_hierarchy_graph_overrides(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=1.425, dec_deg=45.8166667))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        "00057+4549,STF3050,AB,1.425,45.8166667,2024,90,3.0,7.1,8.2\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=10)
    hierarchy.derive_graph("wds", source_id=imported.source_id)
    hierarchy.override_graph_edge(
        provider="wds",
        native_id="00057+4549",
        reference_label="A",
        component_label="B",
        source_id=imported.source_id,
        status="rejected",
        relation_type="cross_link",
        structural_role="non_structural",
        actor="tester",
        reason="exercise graph override in review",
    )

    view = build_review_sky_view(session_factory, target.sdbid)
    segment = next(segment for segment in view.segments if segment.provider == "wds")

    assert segment.status == "rejected"
    assert segment.relation_type == "cross_link"
    assert segment.structural_role == "non_structural"
    assert "persisted hierarchy graph edge" in segment.note
    assert "role non_structural" in segment.note
    assert "exercise graph override in review" in segment.note

    html = render_review_sky_html(view)
    assert "exercise graph override in review" in html
    assert '"name":"hierarchy"' in html
    assert '"relation_type":"cross_link"' in html


def test_review_sky_view_places_hierarchy_candidate_at_nearest_component_endpoint(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    endpoint_ra = base_ra + 3.0 / (3600.0 * math.cos(math.radians(base_dec)))
    target = IdentityService(session_factory).add(AddRequest(ra_deg=endpoint_ra, dec_deg=base_dec))
    path = tmp_path / "wds.csv"
    path.write_text(
        "WDS,Discov,Comp,RAJ2000,DEJ2000,Obs2,PA2,Sep2,Mag1,Mag2\n"
        f"00057+4549,STF3050,AB,{base_ra},{base_dec},2024,90,3.0,7.1,8.2\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=1)

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.kind == "hierarchy")
    html = render_review_sky_html(view)

    assert point.separation_arcsec < 0.01
    assert "plotted at WDS PA2/Sep2 endpoint" in point.note
    assert len(view.segments) == 1
    assert view.segments[0].label == "B"
    assert "B: component link from A" in view.segments[0].note
    assert '"label": "B"' in html


def test_review_sky_view_draws_ccdm_component_links_between_siblings(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=63.8417, dec_deg=-7.6596))
    path = tmp_path / "ccdm.tsv"
    path.write_text(
        "CCDM\trComp\tComp\tRAJ2000\tDEJ2000\tYear\ttheta\trho\tVmag\n"
        "04153-0739\t\tA\t63.8179\t-7.6529\t\t\t\t4.5\n"
        "04153-0739\t\tB\t63.8417\t-7.6596\t1940\t105\t82.8\t9.7\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("ccdm", path, release="test-release")
    hierarchy.match_records("ccdm", source_id=imported.source_id, radius_arcsec=1)

    view = build_review_sky_view(session_factory, target.sdbid)
    segment = next(segment for segment in view.segments if segment.provider == "ccdm")
    html = render_review_sky_html(view)

    assert segment.label == "B"
    assert segment.start_ra_deg == 63.8179
    assert segment.start_dec_deg == -7.6529
    assert segment.end_ra_deg == 63.8417
    assert segment.end_dec_deg == -7.6596
    assert "B: component link from A" in segment.note
    assert "epoch 2000.0" in segment.note
    assert (
        "https://vizier.cds.unistra.fr/viz-bin/VizieR-5?"
        "-out.add=.&-source=I%2F274%2Fccdm&CCDM===04153-0739"
    ) in html


def test_review_sky_view_draws_wds_group_reference_from_midpoint(session_factory, tmp_path):
    base_ra = 2.66066666666667
    base_dec = -73.2243611111111
    b_ra, b_dec = _offset_position(base_ra, base_dec, 0.5, 194.0)
    midpoint_ra, midpoint_dec = _midpoint_position(base_ra, base_dec, b_ra, b_dec)
    target = IdentityService(session_factory).add(AddRequest(ra_deg=base_ra, dec_deg=base_dec))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00106-7313\tI 43\tAB\t00 10 38.56\t-73 13 27.7\t2022\t194\t0.5\n"
        "00106-7313\tI 43\tAB,C\t00 10 38.56\t-73 13 27.7\t2015\t334\t30.4\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=3)

    view = build_review_sky_view(session_factory, target.sdbid)
    segment = next(segment for segment in view.segments if segment.provider == "wds" and segment.label == "C")

    assert segment.start_ra_deg == pytest.approx(midpoint_ra)
    assert segment.start_dec_deg == pytest.approx(midpoint_dec)
    assert segment.end_ra_deg != pytest.approx(b_ra)
    assert segment.relation_type == "group"
    assert "C: component link from AB" in segment.note
    assert "AB midpoint" in segment.note
    assert "reference-group midpoint" in segment.note


def test_review_sky_view_displays_wds_subcomponent_pair_from_first_to_second(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    target = IdentityService(session_factory).add(AddRequest(ra_deg=base_ra, dec_deg=base_dec))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\tAa,Ab\t1.425\t45.8166667\t2024\t90\t0.2\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=3)

    view = build_review_sky_view(session_factory, target.sdbid)
    segment = next(segment for segment in view.segments if segment.provider == "wds")
    point = next(point for point in view.points if point.kind == "hierarchy")

    assert segment.reference_label == "Aa"
    assert segment.label == "Ab"
    assert segment.relation_type == "internal"
    assert point.source_id == "00057+4549 Ab"
    assert "component Aa,Ab" in point.note
    assert "displayed endpoint component Ab" not in point.note


def test_review_sky_view_displays_wds_point_at_latest_pa_sep_endpoint(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    target = IdentityService(session_factory).add(AddRequest(ra_deg=base_ra, dec_deg=base_dec))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\tAB\t1.425\t45.8166667\t2024\t90\t3.0\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=5)

    view = build_review_sky_view(session_factory, target.sdbid)
    point = next(point for point in view.points if point.kind == "hierarchy")

    endpoint_ra, endpoint_dec = _offset_position(base_ra, base_dec, 3.0, 90.0)
    assert point.ra_deg == pytest.approx(endpoint_ra)
    assert point.dec_deg == pytest.approx(endpoint_dec)
    assert point.separation_arcsec == pytest.approx(3.0)
    assert "plotted at WDS PA2/Sep2 endpoint" in point.note


def test_review_sky_view_keeps_wds_subcomponent_chain(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    target = IdentityService(session_factory).add(AddRequest(ra_deg=base_ra, dec_deg=base_dec))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\tAa,Ba\t1.425\t45.8166667\t2024\t90\t1.0\n"
        "00057+4549\tSTF3050\tBa,Bb\t1.4253929\t45.8166667\t2024\t45\t0.2\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=3)

    view = build_review_sky_view(session_factory, target.sdbid)
    segments = {
        (segment.reference_label, segment.component_label): segment
        for segment in view.segments
        if segment.provider == "wds"
    }

    assert ("Aa", "Ba") in segments
    assert ("Ba", "Bb") in segments
    assert segments[("Aa", "Ba")].label == "Ba"
    assert segments[("Aa", "Ba")].relation_type == "group"
    assert segments[("Ba", "Bb")].label == "Bb"
    assert segments[("Ba", "Bb")].relation_type == "internal"


def test_review_sky_view_marks_wds_non_primary_pair_as_group(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    b_ra, b_dec = _offset_position(base_ra, base_dec, 1.0, 90.0)
    target = IdentityService(session_factory).add(AddRequest(ra_deg=b_ra, dec_deg=b_dec))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\tAB\t1.425\t45.8166667\t2024\t90\t1.0\n"
        f"00057+4549\tSTF3050\tBC\t{b_ra}\t{b_dec}\t2024\t45\t0.5\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=3)

    view = build_review_sky_view(session_factory, target.sdbid)
    segment = next(
        segment for segment in view.segments
        if segment.provider == "wds" and segment.reference_label == "B" and segment.component_label == "C"
    )

    assert segment.relation_type == "group"
    assert "C: component link from B" in segment.note


def test_review_sky_view_keeps_unmatched_cross_links_from_same_wds_group(
    session_factory, tmp_path,
):
    base_ra = 1.425
    base_dec = 45.8166667
    b_ra, b_dec = _offset_position(base_ra, base_dec, 30.0, 90.0)
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=base_ra, dec_deg=base_dec)
    )
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        f"00057+4549\tTEST 1\tAB\t{base_ra}\t{base_dec}\t2024\t90\t30.0\n"
        f"00057+4549\tTEST 2\tBE\t{b_ra}\t{b_dec}\t2024\t45\t2.0\n"
        f"00057+4549\tTEST 2\tBF\t{b_ra}\t{b_dec}\t2024\t135\t3.0\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=3)

    view = build_review_sky_view(session_factory, target.sdbid)
    segments = {
        (segment.reference_label, segment.component_label): segment
        for segment in view.segments
        if segment.provider == "wds"
    }
    points = {
        point.source_id: point
        for point in view.points
        if point.provider == "wds"
    }

    assert ("A", "B") in segments
    assert ("B", "E") in segments
    assert ("B", "F") in segments
    assert points["00057+4549 E"].status == "context"
    assert points["00057+4549 E"].candidate_id is None
    assert points["00057+4549 F"].status == "context"
    assert points["00057+4549 F"].candidate_id is None
    assert "same hierarchy group; context only" in segments[("B", "E")].note


def test_review_sky_view_does_not_draw_wds_999_separation_geometry(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    target = IdentityService(session_factory).add(AddRequest(ra_deg=base_ra, dec_deg=base_dec))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\tAC\t1.425\t45.8166667\t2024\t63\t999.9\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=3)

    view = build_review_sky_view(session_factory, target.sdbid)

    assert view.segments == ()
    assert not any(point.kind == "hierarchy" for point in view.points)


def test_review_sky_view_displays_single_blank_wds_component_as_implicit_ab(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    target = IdentityService(session_factory).add(AddRequest(ra_deg=base_ra, dec_deg=base_dec))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\t\t1.425\t45.8166667\t2024\t90\t3.0\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=3)

    view = build_review_sky_view(session_factory, target.sdbid)
    segment = next(segment for segment in view.segments if segment.provider == "wds")
    point = next(point for point in view.points if point.kind == "hierarchy")

    assert segment.reference_label == "A"
    assert segment.component_label == "B"
    assert segment.relation_type == "group"
    assert point.source_id == "00057+4549 B"
    assert "blank WDS component displayed as implicit A-B pair" in point.note


def test_review_sky_view_leaves_blank_wds_component_when_explicit_ab_exists(session_factory, tmp_path):
    base_ra = 1.425
    base_dec = 45.8166667
    target = IdentityService(session_factory).add(AddRequest(ra_deg=base_ra, dec_deg=base_dec))
    path = tmp_path / "wds.tsv"
    path.write_text(
        "WDS\tDiscov\tComp\tRAJ2000\tDEJ2000\tObs2\tPA2\tSep2\n"
        "00057+4549\tSTF3050\t\t1.425\t45.8166667\t2024\t90\t3.0\n"
        "00057+4549\tSTF3050\tAB\t1.425\t45.8166667\t2024\t45\t0.2\n",
        encoding="utf-8",
    )
    hierarchy = HierarchyService(session_factory)
    imported = hierarchy.import_snapshot("wds", path, release="test-release")
    hierarchy.match_records("wds", source_id=imported.source_id, radius_arcsec=3)

    view = build_review_sky_view(session_factory, target.sdbid)
    blank_point = next(
        point for point in view.points
        if point.kind == "hierarchy" and "component AB" not in point.note
    )

    assert "blank WDS component displayed as implicit A-B pair" not in blank_point.note
    assert blank_point.source_id == "00057+4549 STF3050"


def test_review_view_cli_writes_html(tmp_path, capsys):
    from sdb_identity.cli import main

    database = tmp_path / "review.sqlite"
    output = tmp_path / "review.html"
    assert main(["--database", str(database), "init"]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(database), "--offline", "add",
        "--ra", "10", "--dec", "-20",
    ]) == 0
    added = json.loads(capsys.readouterr().out)

    assert main([
        "--database", str(database), "review-view",
        added["sdbid"], "--output", str(output),
    ]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["sdbid"] == added["sdbid"]
    assert result["points"] >= 1
    assert output.exists()
    assert "Plotly.newPlot" in output.read_text()
