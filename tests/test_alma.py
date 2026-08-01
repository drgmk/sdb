from __future__ import annotations

import json

from astropy.time import Time
from sqlalchemy import func, select

from sdb_identity.alma import AlmaSyncService
from sdb_identity.alma_lookup import AlmaLookupService
from sdb_identity.astrometry import propagate_to_epoch
from sdb_identity.cli import main
from sdb_identity.models.alma import (
    AlmaMember,
    AlmaMemberPosition,
    AlmaSyncChunk,
    AlmaSyncRun,
)
from sdb_identity.models.identity import AstrometricSolution
from sdb_identity.providers import ProviderError
from sdb_identity.providers import Astrometry
from sdb_identity.service import AddRequest, IdentityService


class FakeAlmaArchive:
    archive_url = "https://example.invalid/alma"

    def __init__(self, bootstrap_rows=(), incremental_rows=()):
        self.bootstrap_rows = list(bootstrap_rows)
        self.incremental_rows = list(incremental_rows)
        self.chunks = []
        self.watermarks = []
        self.undated_calls = 0

    def bootstrap_chunk(self, start_mjd, end_mjd):
        self.chunks.append((start_mjd, end_mjd))
        rows, self.bootstrap_rows = self.bootstrap_rows, []
        return rows

    def modified_since(self, watermark):
        self.watermarks.append(watermark)
        return self.incremental_rows

    def bootstrap_undated(self):
        self.undated_calls += 1
        return []


class FailingAlmaArchive(FakeAlmaArchive):
    def __init__(self, fail_call):
        super().__init__()
        self.fail_call = fail_call

    def bootstrap_chunk(self, start_mjd, end_mjd):
        self.chunks.append((start_mjd, end_mjd))
        if self.fail_call and len(self.chunks) == self.fail_call:
            raise ProviderError("temporary archive failure", transient=True)
        return []


def alma_row(provider_id="ivo://alma/1", **values):
    row = {
        "obs_publisher_did": provider_id,
        "obs_id": provider_id.rsplit("/", 1)[-1],
        "group_ous_uid": "uid://group/1",
        "member_ous_uid": provider_id,
        "asdm_uid": "uid://asdm/1",
        "proposal_id": "2024.1.00001.S",
        "target_name": "HD 107146",
        "s_ra": 10.0,
        "s_dec": -20.0,
        "s_fov": 20 / 3600,
        "s_region": "CIRCLE ICRS 10 -20 0.0027778",
        "t_min": float(Time("2024-01-01").mjd),
        "t_max": float(Time("2024-01-02").mjd),
        "obs_release_date": "2025-01-01T00:00:00",
        "data_rights": "proprietary",
        "band_list": "6 7",
        "lastModified": "2025-02-01T12:00:00",
    }
    row.update(values)
    return row


def test_alma_bootstrap_stores_band_and_sync_provenance(session_factory):
    provider = FakeAlmaArchive([alma_row()])
    result = AlmaSyncService(session_factory, provider).bootstrap(2024, 2026)

    assert (result.status, result.row_count, result.upserted_count) == (
        "completed", 1, 1,
    )
    assert len(provider.chunks) == 8
    assert provider.undated_calls == 1
    with session_factory() as session:
        observation = session.scalar(select(AlmaMember))
        run = session.scalar(select(AlmaSyncRun))
    assert observation.proposal_id == "2024.1.00001.S"
    assert observation.band_list == "6 7"
    assert observation.data_rights == "proprietary"
    assert run.archive_url == provider.archive_url
    assert run.watermark_after == "2025-02-01T12:00:00"


def test_alma_incremental_uses_last_modified_and_upserts(session_factory):
    provider = FakeAlmaArchive([alma_row()])
    service = AlmaSyncService(session_factory, provider)
    service.bootstrap(2024, 2025)
    provider.incremental_rows = [alma_row(
        band_list="3 6 7", lastModified="2026-03-04T00:00:00",
    )]

    result = service.incremental()

    assert provider.watermarks == ["2025-02-01T12:00:00"]
    assert result.watermark_after == "2026-03-04T00:00:00"
    with session_factory() as session:
        observations = list(session.scalars(select(AlmaMember)))
    assert len(observations) == 1
    assert observations[0].band_list == "3 6 7"


def test_full_bootstrap_deactivates_rows_absent_from_reconciliation(session_factory):
    provider = FakeAlmaArchive([
        alma_row("ivo://alma/1"), alma_row("ivo://alma/2"),
    ])
    service = AlmaSyncService(session_factory, provider)
    service.bootstrap(2024, 2025)
    provider.bootstrap_rows = [alma_row("ivo://alma/1")]

    result = service.bootstrap(2024, 2025)

    assert result.deactivated_count == 1
    with session_factory() as session:
        states = {
            value.member_ous_uid: value.active
            for value in session.scalars(select(AlmaMember))
        }
    assert sorted(states.values()) == [False, True]


def test_alma_project_lookup_propagates_target_to_observation_epoch(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=20))
    with session_factory.begin() as session:
        solution = session.get(AstrometricSolution, target.target_id)
        solution.epoch = 2000.0
        solution.ra_deg = 10.0
        solution.dec_deg = 20.0
        solution.pm_ra_cosdec_masyr = 1000.0
        solution.pm_dec_masyr = -500.0
        solution.proper_motion_available = True
    native = Astrometry(10, 20, 2000, 1000, -500)
    moved = propagate_to_epoch(native, 2024.0)
    provider = FakeAlmaArchive([alma_row(
        s_ra=moved.ra_deg,
        s_dec=moved.dec_deg,
        t_min=float(Time(2024.0, format="jyear").mjd),
        t_max=float(Time(2024.0, format="jyear").mjd),
        band_list="7",
    )])
    service = AlmaSyncService(session_factory, provider)
    service.bootstrap(2024, 2025)

    projects = AlmaLookupService(session_factory).projects(target.target_id)

    assert len(projects) == 1
    assert projects[0].proposal_id == "2024.1.00001.S"
    assert projects[0].band_lists == ("7",)


def test_alma_projects_cli_is_local(session_factory, db_path, capsys):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    AlmaSyncService(
        session_factory, FakeAlmaArchive([alma_row()]),
    ).bootstrap(2024, 2025)

    assert main([
        "--database", str(db_path), "alma", "projects", target.sdbid,
    ]) == 0
    project = json.loads(capsys.readouterr().out)
    assert project["proposal_id"] == "2024.1.00001.S"
    assert project["band_lists"] == ["6", "7"]


def test_alma_rows_deduplicate_to_member_with_all_positions_and_bands(
    session_factory,
):
    rows = [
        alma_row("ivo://product/1", member_ous_uid="uid://member/1", band_list="6"),
        alma_row(
            "ivo://product/2", member_ous_uid="uid://member/1",
            s_ra=10.1, band_list="7",
        ),
    ]
    AlmaSyncService(
        session_factory, FakeAlmaArchive(rows),
    ).bootstrap(2024, 2025)

    with session_factory() as session:
        members = list(session.scalars(select(AlmaMember)))
        position_count = session.scalar(
            select(func.count(AlmaMemberPosition.id))
        )
    assert len(members) == 1
    assert members[0].member_ous_uid == "uid://member/1"
    assert members[0].band_list == "6 7"
    assert position_count == 2


def test_alma_member_uid_is_unique_within_proposal(session_factory):
    rows = [
        alma_row(member_ous_uid="uid://member/reused"),
        alma_row(
            "ivo://product/other",
            member_ous_uid="uid://member/reused",
            proposal_id="2024.1.00002.S",
        ),
    ]
    AlmaSyncService(
        session_factory, FakeAlmaArchive(rows),
    ).bootstrap(2024, 2025)

    with session_factory() as session:
        members = list(session.scalars(
            select(AlmaMember).order_by(AlmaMember.proposal_id)
        ))
    assert [member.proposal_id for member in members] == [
        "2024.1.00001.S",
        "2024.1.00002.S",
    ]
    assert {member.member_ous_uid for member in members} == {
        "uid://member/reused"
    }


def test_failed_alma_bootstrap_resumes_only_unfinished_chunks(session_factory):
    provider = FailingAlmaArchive(fail_call=3)
    service = AlmaSyncService(session_factory, provider)
    try:
        service.bootstrap(2024, 2026, chunk_months=3)
    except ProviderError:
        pass
    else:
        raise AssertionError("expected synthetic archive failure")

    with session_factory() as session:
        run = session.scalar(select(AlmaSyncRun))
        statuses = list(session.scalars(
            select(AlmaSyncChunk.status).order_by(AlmaSyncChunk.id)
        ))
    assert run.status == "failed"
    assert statuses[:3] == ["completed", "completed", "failed"]

    provider.fail_call = None
    result = service.resume(run.id, start_year=2024, end_year=2026, chunk_months=3)
    assert result.status == "completed"
    # Two dated chunks were already complete; seven entries remained including
    # the failed dated chunk and the final undated chunk.
    assert len(provider.chunks) == 3 + 6
    assert provider.undated_calls == 1
