from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import select

from sdb_identity.astrometry import propagate_to_epoch
from sdb_identity.identity_results import effective_identity_candidate_ids
from sdb_identity.models.identity import (
    AstrometricSolution,
    ExternalIdentifier,
    MatchCandidate,
    ProviderOutcome,
    Submission,
    Target,
)
from sdb_identity.service import AddRequest, IdentityService, UnresolvedTarget
from tests.fakes import FakeGaia, FakeSimbad, astrometry, gaia_candidate, simbad_result


def service(session_factory, *, simbad=None, gaia=None):
    return IdentityService(
        session_factory,
        simbad=simbad or FakeSimbad(),
        gaia=gaia or FakeGaia(),
    )


def test_coordinate_only_source_needs_no_remote_match(session_factory):
    result = service(session_factory).add(AddRequest(ra_deg=12.3, dec_deg=-45.6))
    assert result.created is True
    assert result.sdbid.startswith("sdbid-v3-")
    assert result.astrometry_source == "input"


def test_simbad_only_source(session_factory):
    simbad = FakeSimbad({"Star": simbad_result("Star", astrometry(10, 20, source="simbad"), ("HD 1",))})
    result = service(session_factory, simbad=simbad).add(AddRequest(name="Star"))
    assert result.astrometry_source == "simbad"


def test_simbad_astrometry_bibliography_is_stored_with_solution(session_factory):
    value = astrometry(10, 20, pmra=5, pmdec=2, source="simbad")
    value = replace(
        value,
        position_bibcode="2020A&A...000....1A",
        proper_motion_bibcode="2020A&A...000....2B",
        parallax_bibcode="2020A&A...000....3C",
        radial_velocity_bibcode="2020A&A...000....4D",
    )
    simbad = FakeSimbad({"Star": simbad_result("Star", value)})
    result = service(session_factory, simbad=simbad).add(AddRequest(name="Star"))
    with session_factory() as session:
        solution = session.scalar(select(AstrometricSolution).where(
            AstrometricSolution.target_id == result.target_id
        ))
        assert solution.position_bibcode == "2020A&A...000....1A"
        assert solution.proper_motion_bibcode == "2020A&A...000....2B"
        assert solution.parallax_bibcode == "2020A&A...000....3C"
        assert solution.radial_velocity_bibcode == "2020A&A...000....4D"


def test_gaia_is_preferred_for_coordinate_source(session_factory):
    gaia = FakeGaia([gaia_candidate("123", astrometry(10.00001, 20, epoch=2016, pmra=5, pmdec=2, source="gaia_dr3"))])
    result = service(session_factory, gaia=gaia).add(AddRequest(ra_deg=10, dec_deg=20))
    assert result.astrometry_source == "gaia_dr3"
    with session_factory() as session:
        candidate = session.scalar(select(MatchCandidate))
        assert candidate.proper_motion_available
        assert candidate.pm_ra_cosdec_masyr == 5
        assert candidate.pm_dec_masyr == 2


def test_consistent_named_coordinates_use_richer_simbad_pm_for_gaia_search(
    session_factory,
):
    simbad = FakeSimbad({
        "Fast star": simbad_result(
            "Fast star",
            astrometry(10, 20, epoch=2000, pmra=5000, pmdec=-2000, source="simbad"),
            ("2MASS J00400000+2000000",),
        ),
    })
    gaia_astrometry = astrometry(
        10.02365, 19.99111, epoch=2016, pmra=5000, pmdec=-2000,
        source="gaia_dr3",
    )
    gaia = FakeGaia([gaia_candidate("123", gaia_astrometry)])
    result = service(session_factory, simbad=simbad, gaia=gaia).add(
        AddRequest(name="Fast star", ra_deg=10, dec_deg=20, epoch=2000)
    )
    assert result.astrometry_source == "gaia_dr3"


def test_unaccepted_gaia_candidates_keep_richer_simbad_pm_solution(session_factory):
    simbad_astrometry = astrometry(
        10,
        20,
        epoch=2000,
        pmra=5000,
        pmdec=-2000,
        source="simbad",
    )
    simbad_astrometry = replace(
        simbad_astrometry,
        proper_motion_bibcode="2007A&A...474..653V",
    )
    moved = propagate_to_epoch(simbad_astrometry, 2016.0)
    simbad = FakeSimbad({
        "Fast binary": simbad_result("Fast binary", simbad_astrometry),
    })
    gaia = FakeGaia([
        gaia_candidate(
            "near-a",
            astrometry(moved.ra_deg + 0.00030, moved.dec_deg, epoch=2016.0, source="gaia_dr3"),
        ),
        gaia_candidate(
            "near-b",
            astrometry(moved.ra_deg + 0.00031, moved.dec_deg, epoch=2016.0, source="gaia_dr3"),
        ),
    ])

    result = service(session_factory, simbad=simbad, gaia=gaia).add(
        AddRequest(name="Fast binary", ra_deg=10, dec_deg=20, epoch=2000)
    )

    assert result.astrometry_source == "simbad"
    with session_factory() as session:
        solution = session.scalar(select(AstrometricSolution))
        assert solution.source == "simbad"
        assert solution.pm_ra_cosdec_masyr == 5000
        assert solution.pm_dec_masyr == -2000
        assert solution.proper_motion_bibcode == "2007A&A...474..653V"
        assert effective_identity_candidate_ids(
            session, target_ids=[result.target_id],
        ) == set()
        assert all(
            not candidate.proper_motion_available
            and candidate.pm_ra_cosdec_masyr is None
            and candidate.proper_motion_bibcode is None
            for candidate in session.scalars(select(MatchCandidate))
        )


def test_inconsistent_named_coordinates_remain_explicit(session_factory):
    simbad = FakeSimbad({
        "Other star": simbad_result(
            "Other star",
            astrometry(11, 20, epoch=2000, pmra=5000, pmdec=-2000, source="simbad"),
        ),
    })
    result = service(session_factory, simbad=simbad).add(
        AddRequest(name="Other star", ra_deg=10, dec_deg=20, epoch=2000)
    )
    assert result.astrometry_source == "input"


def test_unresolvable_name_records_failure_without_target(session_factory):
    with pytest.raises(UnresolvedTarget):
        service(session_factory).add(AddRequest(name="Not a source"))
    with session_factory() as session:
        assert session.query(Target).count() == 0
        submission = session.scalars(select(Submission)).one()
        assert submission.status == "failed"


def test_repeat_submission_returns_existing_target(session_factory):
    svc = service(session_factory)
    first = svc.add(AddRequest(ra_deg=10, dec_deg=20))
    second = svc.add(AddRequest(ra_deg=10, dec_deg=20))
    assert first.target_id == second.target_id
    assert second.created is False


def test_concurrent_equivalent_submissions_create_one_target(session_factory):
    svc = service(session_factory)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: svc.add(AddRequest(ra_deg=10, dec_deg=20)), range(2)))
    assert len({result.target_id for result in results}) == 1
    with session_factory() as session:
        assert session.query(Target).count() == 1


def test_nearby_source_is_deduplicated(session_factory):
    svc = service(session_factory)
    first = svc.add(AddRequest(ra_deg=10, dec_deg=20))
    second = svc.add(AddRequest(ra_deg=10.00001, dec_deg=20))
    assert first.target_id == second.target_id


def test_nearby_simbad_component_is_not_collapsed_by_shared_system_aliases(session_factory):
    simbad = FakeSimbad({
        "HD 224953": simbad_result(
            "HD 224953",
            astrometry(0.53640043, -68.28076443, source="simbad"),
            ("HD 224953A", "WDS J00021-6817AB"),
        ),
        "HD 224953A": simbad_result(
            "HD 224953A",
            astrometry(0.53636746, -68.28076428, source="simbad"),
            ("HD 224953", "WDS J00021-6817A"),
        ),
    })
    identity = service(session_factory, simbad=simbad)

    system = identity.add(AddRequest(name="HD 224953"))
    component = identity.add(AddRequest(name="HD 224953A"))
    repeated = identity.add(AddRequest(name="HD 224953A"))

    assert component.target_id != system.target_id
    assert component.sdbid != system.sdbid
    assert not component.sdbid.endswith("-A")
    assert repeated.target_id == component.target_id
    assert repeated.created is False
    with session_factory() as session:
        hd_a = list(session.scalars(select(ExternalIdentifier).where(
            ExternalIdentifier.normalized_value == "HD 224953A"
        )))
        assert [(row.target_id, row.value) for row in hd_a] == [
            (component.target_id, "HD 224953A")
        ]


def test_true_positional_sdbid_collision_uses_component_qualifier(session_factory):
    simbad = FakeSimbad({
        "Pair": simbad_result("HD 1", astrometry(10.0, 20.0, source="simbad")),
        "Pair A": simbad_result(
            "HD 1A",
            astrometry(10.000001, 20.000001, source="simbad"),
            ("HD 1",),
        ),
    })
    identity = service(session_factory, simbad=simbad)

    system = identity.add(AddRequest(name="Pair"))
    component = identity.add(AddRequest(name="Pair A"))

    assert component.target_id != system.target_id
    assert component.sdbid == f"{system.sdbid}-A"


def test_ambiguous_candidates_are_retained_but_not_selected(session_factory):
    gaia = FakeGaia([
        gaia_candidate("1", astrometry(10.00010, 20, epoch=2016, source="gaia_dr3")),
        gaia_candidate("2", astrometry(10.00011, 20, epoch=2016, source="gaia_dr3")),
    ])
    result = service(session_factory, gaia=gaia).add(AddRequest(ra_deg=10, dec_deg=20))
    assert result.astrometry_source == "input"
    with session_factory() as session:
        candidates = session.scalars(select(MatchCandidate)).all()
        assert len(candidates) == 2
        assert effective_identity_candidate_ids(
            session, target_ids=[result.target_id],
        ) == set()
        assert all(candidate.id for candidate in candidates)


def test_simbad_gaia_dr3_identifier_beats_nearer_unlisted_gaia_candidate(session_factory):
    simbad = FakeSimbad({
        "HD 3405": simbad_result(
            "HD 3405",
            astrometry(10.0, 20.0, source="simbad"),
            ("Gaia DR3 111",),
        ),
    })
    gaia = FakeGaia([
        gaia_candidate("222", astrometry(10.0002, 20.0, epoch=2016, source="gaia_dr3")),
        gaia_candidate("111", astrometry(10.0012, 20.0, epoch=2016, source="gaia_dr3")),
    ])

    result = service(session_factory, simbad=simbad, gaia=gaia).add(AddRequest(name="HD 3405"))

    assert result.astrometry_source == "gaia_dr3"
    with session_factory() as session:
        solution = session.scalar(select(AstrometricSolution))
        assert solution.source_id == "111"
        candidates = {
            candidate.source_id: candidate
            for candidate in session.scalars(select(MatchCandidate))
        }
        assert effective_identity_candidate_ids(
            session, target_ids=[result.target_id],
        ) == {candidates["111"].id}
        assert candidates["111"].score > candidates["222"].score


def test_gaia_identifier_match_without_pm_keeps_richer_simbad_solution(session_factory):
    simbad_astrometry = astrometry(
        10.0,
        20.0,
        epoch=2000.0,
        pmra=250.0,
        pmdec=-125.0,
        source="simbad",
    )
    simbad = FakeSimbad({
        "HD 2475": simbad_result(
            "HD 2475",
            simbad_astrometry,
            ("Gaia DR3 2363291052952046208",),
        ),
    })
    moved = propagate_to_epoch(simbad_astrometry, 2016.0)
    gaia = FakeGaia([
        gaia_candidate(
            "2363291052952046208",
            astrometry(moved.ra_deg, moved.dec_deg, epoch=2016.0, source="gaia_dr3"),
        ),
    ])

    result = service(session_factory, simbad=simbad, gaia=gaia).add(AddRequest(name="HD 2475"))

    assert result.astrometry_source == "simbad"
    with session_factory() as session:
        target = session.get(Target, result.target_id)
        canonical = session.get(AstrometricSolution, target.canonical_astrometry_id)
        assert canonical.source == "simbad"
        assert canonical.pm_ra_cosdec_masyr == 250.0
        assert canonical.pm_dec_masyr == -125.0
        solutions = session.scalars(
            select(AstrometricSolution).where(AstrometricSolution.target_id == target.id)
        ).all()
        assert {(solution.source, solution.source_id) for solution in solutions} == {
            ("simbad", "HD 2475"),
            ("gaia_dr3", "2363291052952046208"),
        }
        candidate = session.scalar(select(MatchCandidate))
        assert effective_identity_candidate_ids(
            session, target_ids=[result.target_id],
        ) == {candidate.id}
        assert candidate.source_id == "2363291052952046208"
        assert candidate.proper_motion_available is False


def test_gaia_candidate_scoring_compares_at_gaia_epoch_for_high_pm_simbad_source(session_factory):
    simbad_astrometry = astrometry(
        10.0,
        20.0,
        epoch=2000.0,
        pmra=400.0,
        pmdec=0.0,
        source="simbad",
    )
    moved = propagate_to_epoch(simbad_astrometry, 2016.0)
    simbad = FakeSimbad({
        "Moving star": simbad_result("Moving star", simbad_astrometry),
    })
    gaia = FakeGaia([
        gaia_candidate(
            "stale-position",
            astrometry(10.0, 20.0, epoch=2016.0, source="gaia_dr3"),
        ),
        gaia_candidate(
            "propagated-position",
            astrometry(moved.ra_deg, moved.dec_deg, epoch=2016.0, source="gaia_dr3"),
        ),
    ])

    result = service(session_factory, simbad=simbad, gaia=gaia).add(
        AddRequest(name="Moving star")
    )

    assert result.astrometry_source == "simbad"
    with session_factory() as session:
        target = session.get(Target, result.target_id)
        canonical = session.get(AstrometricSolution, target.canonical_astrometry_id)
        assert canonical.source == "simbad"
        assert canonical.pm_ra_cosdec_masyr == 400.0
        solutions = session.scalars(
            select(AstrometricSolution).where(AstrometricSolution.target_id == target.id)
        ).all()
        assert {(solution.source, solution.source_id) for solution in solutions} == {
            ("simbad", "Moving star"),
            ("gaia_dr3", "propagated-position"),
        }
        candidates = {
            candidate.source_id: candidate
            for candidate in session.scalars(select(MatchCandidate))
        }
        assert candidates["propagated-position"].separation_arcsec < 0.001
        assert effective_identity_candidate_ids(
            session, target_ids=[result.target_id],
        ) == {candidates["propagated-position"].id}
        assert candidates["propagated-position"].proper_motion_available is False


def test_timeout_is_distinct_from_no_match(session_factory):
    result = service(session_factory, gaia=FakeGaia(error="timeout")).add(AddRequest(ra_deg=10, dec_deg=20))
    with session_factory() as session:
        outcome = session.scalars(select(ProviderOutcome).where(ProviderOutcome.provider == "gaia_dr3")).one()
        assert outcome.status == "transient_failure"
        assert result.astrometry_source == "input"


def test_manual_override_appends_audit_history(session_factory):
    gaia = FakeGaia([
        gaia_candidate(
            "1", astrometry(10.00010, 20, epoch=2016, source="gaia_dr3"),
        ),
        gaia_candidate(
            "2", astrometry(10.00011, 20, epoch=2016, source="gaia_dr3"),
        ),
    ])
    svc = service(session_factory, gaia=gaia)
    added = svc.add(AddRequest(ra_deg=10, dec_deg=20))
    with session_factory() as session:
        candidate = session.scalar(
            select(MatchCandidate).where(MatchCandidate.source_id == "2")
        )
        candidate_id = candidate.id
    svc.override_match(candidate_id, actor="tester", reason="manual check")
    history = svc.match_history(candidate_id)
    assert [entry.decision for entry in history] == ["deferred", "accepted"]
    assert history[-1].method == "manual"
    with session_factory() as session:
        target = session.get(Target, added.target_id)
        canonical = session.get(
            AstrometricSolution, target.canonical_astrometry_id,
        )
        identifiers = set(session.scalars(
            select(ExternalIdentifier.value).where(
                ExternalIdentifier.target_id == target.id
            )
        ))
        assert canonical.source == "gaia_dr3"
        assert canonical.source_id == "2"
        assert "Gaia DR3 2" in identifiers
        assert effective_identity_candidate_ids(
            session, target_ids=[target.id],
        ) == {candidate_id}

    svc.override_match(
        candidate_id, actor="tester", reason="repeat submission",
    )
    assert len(svc.match_history(candidate_id)) == 2
