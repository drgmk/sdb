from __future__ import annotations

from sqlalchemy import select

from sdb_identity.catalogs.results import effective_catalog_results
from sdb_identity.catalogs.acquisition import CatalogAcquisitionService
from sdb_identity.models.catalogs import (
    CatalogResultDecision,
    CatalogRun,
    RawCatalogRow,
)
from sdb_identity.review.actions import review_catalog_target_association_decision
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement


def test_latest_decision_interprets_one_immutable_ambiguous_run(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    service = CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("one", ra=10.00010, measurements=[measurement()]),
            candidate(
                "two", ra=10.00011,
                measurements=[measurement(value=8.2)],
            ),
        ]),
    })
    acquired = service.refresh(target.sdbid, "2mass")
    assert acquired.status == "ambiguous"

    with session_factory.begin() as session:
        run = session.get(CatalogRun, acquired.run_id)
        chosen = session.scalar(
            select(RawCatalogRow)
            .where(
                RawCatalogRow.run_id == run.id,
                RawCatalogRow.source_id == "two",
            )
        )
        session.add(CatalogResultDecision(
            target_id=target.target_id,
            provider="2mass",
            reviewed_run_id=run.id,
            action="accept_detection",
            accepted_detection_id=chosen.detection_id,
            reviewed_raw_row_id=chosen.id,
            actor="reviewer",
            reason="identified from the image",
        ))

    with session_factory() as session:
        result = effective_catalog_results(
            session, [target.target_id], providers=["2mass"],
        )[(target.target_id, "2mass")]
        run_count = session.query(CatalogRun).count()
        raw_count = session.query(RawCatalogRow).count()
    assert result.status == "match"
    assert result.run.id == acquired.run_id
    assert result.selected_source_id == "two"
    assert (run_count, raw_count) == (1, 2)

    with session_factory.begin() as session:
        session.add(CatalogResultDecision(
            target_id=target.target_id,
            provider="2mass",
            reviewed_run_id=acquired.run_id,
            action="reviewed_no_match",
            actor="reviewer",
            reason="revised after checking both candidates",
        ))
    with session_factory() as session:
        result = effective_catalog_results(
            session, [target.target_id], providers=["2mass"],
        )[(target.target_id, "2mass")]
    assert result.status == "no_match"
    assert result.selected_detection is None
    assert result.decision.reason == "revised after checking both candidates"


def test_accepted_source_association_resolves_ambiguous_provider_result(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    acquired = CatalogAcquisitionService(session_factory, {
        "2mass": FakeCatalog([
            candidate("one", ra=10.00010, measurements=[measurement()]),
            candidate("two", ra=10.00011, measurements=[measurement(value=8.2)]),
        ]),
    }).refresh(target.sdbid, "2mass")
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
        actor="reviewer",
        reason="identified from the image",
        expected_token=preview["state_token"],
    )

    with session_factory() as session:
        result = effective_catalog_results(
            session, [target.target_id], providers=["2mass"],
        )[(target.target_id, "2mass")]
        stored_run = session.get(CatalogRun, acquired.run_id)

    assert stored_run.status == "ambiguous"
    assert result.status == "match"
    assert result.selected_source_id == "two"
    assert result.selected_raw_row.id == chosen.id
    assert result.decision is None
    assert result.association_action.action == "accept"
