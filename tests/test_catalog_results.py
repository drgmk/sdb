from __future__ import annotations

from sqlalchemy import select

from sdb_identity.catalog_results import effective_catalog_results
from sdb_identity.catalogs import CatalogService
from sdb_identity.models import (
    CatalogResultDecision,
    CatalogRun,
    RawCatalogRow,
)
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement


def test_latest_decision_interprets_one_immutable_ambiguous_run(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    service = CatalogService(session_factory, {
        "2mass": FakeCatalog([
            candidate("one", ra=10.00010, measurements=[measurement()]),
            candidate("two", ra=10.00011, measurements=[measurement(value=8.2)]),
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
