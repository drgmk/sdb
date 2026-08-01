from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select, text

from sdb_identity.cli import main
from sdb_identity.database import make_session_factory
from sdb_identity.dirty import pending_export_targets
from sdb_identity.models.samples import SampleMembershipAction
from sdb_identity.samples.service import SampleService
from sdb_identity.service import AddRequest, IdentityService


def _targets(session_factory):
    service = IdentityService(session_factory)
    return (
        service.add(AddRequest(ra_deg=10, dec_deg=-20)),
        service.add(AddRequest(ra_deg=30, dec_deg=-40)),
    )


def test_samples_group_targets_many_to_many_with_metadata(session_factory):
    first, second = _targets(session_factory)
    samples = SampleService(session_factory)
    samples.create("nearby", sample_date="2026-07-06", note="Initial sample")
    samples.create("follow-up")

    samples.add("nearby", first.sdbid, actor="grant", reason="sample definition")
    samples.add("nearby", second.sdbid, actor="grant", reason="sample definition")
    samples.add("follow-up", first.sdbid, actor="grant", reason="observe again")

    summaries = {value.name: value for value in samples.list()}
    assert summaries["nearby"].member_count == 2
    assert summaries["nearby"].sample_date.isoformat() == "2026-07-06"
    assert summaries["nearby"].note == "Initial sample"
    assert [target.id for target in samples.members("follow-up")] == [first.target_id]


def test_membership_removal_and_readdition_preserve_history(session_factory):
    first, _ = _targets(session_factory)
    samples = SampleService(session_factory)
    samples.create("variable")
    samples.add("variable", first.target_id, actor="grant", reason="selected")
    samples.remove("variable", first.target_id, actor="grant", reason="selection revised")
    assert samples.members("variable") == []
    samples.add("variable", first.target_id, actor="grant", reason="selection restored")

    with session_factory() as session:
        actions = list(session.scalars(select(SampleMembershipAction.action)))
        current = session.execute(text(
            "SELECT sdbid FROM current_sample_memberships WHERE sample_name='variable'"
        )).scalar_one()
    assert actions == ["add", "remove", "add"]
    assert current == first.sdbid


def test_sample_import_is_atomic_when_a_target_is_missing(
    session_factory, tmp_path,
):
    first, _ = _targets(session_factory)
    samples = SampleService(session_factory)
    samples.create("imported")
    path = tmp_path / "members.csv"
    path.write_text(f"sdbid\n{first.sdbid}\nnot-a-target\n", encoding="utf-8")

    with pytest.raises(KeyError, match="target not found"):
        samples.import_members(
            "imported", path, actor="grant", reason="project list",
        )

    with session_factory() as session:
        assert session.scalar(select(func.count(SampleMembershipAction.id))) == 0


def test_sample_cli_create_add_and_list_members(db_path, capsys):
    target = IdentityService(make_session_factory(db_path)).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )

    assert main([
        "--database", str(db_path), "sample", "create", "survey",
        "--date", "2026-07-06", "--note", "Working selection",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(db_path), "sample", "add", "survey", target.sdbid,
        "--actor", "grant", "--reason", "selected",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--database", str(db_path), "sample", "members", "survey",
    ]) == 0
    member = json.loads(capsys.readouterr().out)
    assert member == {"sdbid": target.sdbid, "target_id": target.target_id}


def test_dirty_targets_can_be_limited_to_current_sample_members(session_factory):
    first, second = _targets(session_factory)
    samples = SampleService(session_factory)
    samples.create("selected")
    samples.add("selected", first.target_id, actor="grant", reason="selected")

    assert [value[0].id for value in pending_export_targets(
        session_factory, sample="selected",
    )] == [first.target_id]

    samples.remove("selected", first.target_id, actor="grant", reason="removed")
    samples.add("selected", second.target_id, actor="grant", reason="replacement")
    assert [value[0].id for value in pending_export_targets(
        session_factory, sample="selected",
    )] == [second.target_id]


def test_export_dirty_cli_limits_work_to_sample(db_path, tmp_path, capsys):
    sessions = make_session_factory(db_path)
    first, second = _targets(sessions)
    samples = SampleService(sessions)
    samples.create("selected")
    samples.add("selected", first.target_id, actor="grant", reason="selected")

    assert main([
        "--database", str(db_path), "export-dirty", "--sample", "selected",
        "--output-dir", str(tmp_path / "exports"),
    ]) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[-1] == {"exported": 1, "failed": 0}
    assert output[0]["target_id"] == first.target_id
    assert [value[0].id for value in pending_export_targets(sessions)] == [
        second.target_id,
    ]
