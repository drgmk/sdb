from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import select

from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.metadata import MetadataQueryResult, MetadataService
from sdb_identity.models import CatalogRun, MetadataRun
from sdb_identity.service import AddRequest, IdentityService
from tests.fakes import FakeSimbad
from tests.test_catalog import FakeCatalog
from tests.test_metadata import FakeMetadataProvider, snapshot


class BarrierGaia:
    name = "gaia_dr3"

    def __init__(self, barrier):
        self.barrier = barrier

    def search(self, astrometry):
        self.barrier.wait(timeout=3)
        return []


class BarrierCatalog(FakeCatalog):
    def __init__(self, barrier):
        super().__init__([])
        self.barrier = barrier

    def query(self, context):
        self.barrier.wait(timeout=3)
        return []


def test_identity_remote_calls_do_not_hold_sqlite_write_lock(session_factory):
    barrier = Barrier(2)
    service = IdentityService(session_factory, simbad=FakeSimbad(), gaia=BarrierGaia(barrier))
    requests = [AddRequest(ra_deg=10, dec_deg=-20), AddRequest(ra_deg=20, dec_deg=-30)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(service.add, requests))
    assert len(results) == 2


def test_catalog_remote_calls_do_not_hold_sqlite_write_lock(session_factory):
    identity = IdentityService(session_factory)
    targets = [
        identity.add(AddRequest(ra_deg=10, dec_deg=-20)),
        identity.add(AddRequest(ra_deg=20, dec_deg=-30)),
    ]
    barrier = Barrier(2)
    service = CatalogAcquisitionService(session_factory, {"2mass": BarrierCatalog(barrier)})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda target: service.refresh(target.sdbid, "2mass"), targets))
    assert len(results) == 2


def test_new_catalog_attempt_retires_interrupted_running_attempt(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    with session_factory.begin() as session:
        session.add(CatalogRun(
            target_id=target.target_id,
            provider="2mass",
            release="old",
            status="running",
            is_current=False,
            query_ra_deg=10,
            query_dec_deg=-20,
            query_epoch=2000,
        ))

    CatalogAcquisitionService(session_factory, {"2mass": FakeCatalog([])}).refresh(
        target.target_id, "2mass"
    )

    with session_factory() as session:
        runs = list(session.scalars(
            select(CatalogRun).order_by(CatalogRun.id)
        ))
    assert [run.status for run in runs] == ["transient_failure", "no_match"]
    assert runs[0].error == "superseded after interrupted refresh"
    assert runs[0].completed_at is not None


def test_new_metadata_attempt_retires_interrupted_running_attempt(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    with session_factory.begin() as session:
        session.add(MetadataRun(
            target_id=target.target_id,
            provider="simbad",
            release="old",
            status="running",
            is_current=False,
        ))

    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (snapshot(),))),
    ).refresh(target.target_id)

    with session_factory() as session:
        runs = list(session.scalars(
            select(MetadataRun).order_by(MetadataRun.id)
        ))
    assert [run.status for run in runs] == ["transient_failure", "match"]
    assert runs[0].error == "superseded after interrupted refresh"
    assert runs[0].completed_at is not None
