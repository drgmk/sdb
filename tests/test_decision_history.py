from __future__ import annotations

import json

from sqlalchemy import select

from sdb_identity.cli import main
from sdb_identity.database import make_session_factory
from sdb_identity.decision_history import system_decision_history
from sdb_identity.hierarchy.service import HierarchyService
from sdb_identity.models.curated import CuratedAssociationAction
from sdb_identity.models.photometry import MeasurementEligibilityAction
from sdb_identity.models.catalogs import NormalizedMeasurement
from sdb_identity.catalogs.acquisition import CatalogAcquisitionService
from sdb_identity.samples import SampleService
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.target_lifecycle import set_target_lifecycle
from tests.test_catalog import FakeCatalog, candidate, measurement


def test_system_history_is_assembled_from_domain_actions(session_factory):
    identity = IdentityService(session_factory)
    primary = identity.add(AddRequest(ra_deg=10, dec_deg=-20))
    companion = identity.add(AddRequest(ra_deg=10.001, dec_deg=-20))
    hierarchy = HierarchyService(session_factory)
    hierarchy.create_system("history AB", primary=primary.sdbid)
    hierarchy.add_member("history AB", companion.sdbid, component_label="B")
    set_target_lifecycle(
        session_factory,
        companion.sdbid,
        role="physical",
        state="active",
        actor="reviewer",
    )
    samples = SampleService(session_factory)
    samples.create("history")
    samples.add("history", primary.sdbid, actor="reviewer")
    CatalogAcquisitionService(session_factory, {
        "allwise": FakeCatalog(
            [candidate(
                "history-wise",
                measurements=[measurement("WISE3P4", 8.1)],
            )],
            name="allwise",
            release="test",
        ),
    }).refresh(companion.sdbid, "allwise")
    with session_factory() as session, session.begin():
        measurement_id = session.scalar(
            select(NormalizedMeasurement.id).where(
                NormalizedMeasurement.target_id == companion.target_id
            )
        )
        session.add(CuratedAssociationAction(
            dataset="example",
            record_no=7,
            action="associate",
            target_id=companion.target_id,
            actor="reviewer",
            reason="Associated example record 7 with companion",
        ))
        session.add(MeasurementEligibilityAction(
            measurement_id=measurement_id,
            excluded=True,
            actor="reviewer",
            reason="Excluded example record 7 photometry",
        ))

    rows = system_decision_history(session_factory, primary.sdbid)
    assert {row["domain"] for row in rows} == {
        "curated_association",
        "photometry_eligibility",
        "sample_membership",
        "target_lifecycle",
    }
    assert all(row["reason"] for row in rows)

    target_rows = system_decision_history(
        session_factory, primary.sdbid, include_system=False,
    )
    assert [row["domain"] for row in target_rows] == ["sample_membership"]


def test_history_cli_emits_normalized_json(db_path, capsys, monkeypatch):
    monkeypatch.setenv("SDB_ACTOR", "reviewer")
    target = IdentityService(make_session_factory(db_path)).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    assert main([
        "--database", str(db_path), "sample", "create", "history",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(db_path), "sample", "add", "history", target.sdbid,
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(db_path), "history", target.sdbid,
    ]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["domain"] == "sample_membership"
    assert row["actor"] == "reviewer"
    assert row["reason"] == f"Added {target.sdbid} to sample history"
