from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from sdb_identity.batch import BatchService
from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.metadata import MetadataQueryResult, MetadataService
from sdb_identity.models.batch import ImportItem, ImportJob, ImportRun
from sdb_identity.models.identity import Target
from sdb_identity.providers import ProviderError
from sdb_identity.service import IdentityService
from tests.fakes import FakeGaia, FakeSimbad, astrometry, simbad_result
from tests.test_catalog import FakeBulkCatalog, FakeCatalog
from tests.test_metadata import FakeMetadataProvider


def write_targets(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def factories(session_factory, *, metadata_provider=None):
    simbad = FakeSimbad({
        "Star": simbad_result("Star", astrometry(10, -20, source="simbad")),
    })
    return {
        "identity_factory": lambda: IdentityService(session_factory, simbad=simbad, gaia=FakeGaia()),
        "metadata_factory": lambda: MetadataService(
            session_factory,
            metadata_provider or FakeMetadataProvider(MetadataQueryResult("no_match")),
        ),
        "catalog_factory": lambda: CatalogAcquisitionService(
            session_factory,
            {"2mass": FakeCatalog([])},
        ),
    }


class FakeBulkSimbad(FakeSimbad):
    def __init__(self, resolutions=None, error: str | None = None):
        super().__init__(resolutions, error)
        self.bulk_calls = []
        self.single_calls = []

    def resolve_many(self, names):
        self.bulk_calls.append(tuple(names))
        if self.error:
            raise ProviderError(self.error, transient=True)
        return {name: self.resolutions.get(name) for name in names}

    def resolve_name(self, name: str):
        self.single_calls.append(name)
        return super().resolve_name(name)


def test_csv_batch_runs_dependency_order_and_deduplicates_targets(session_factory, tmp_path):
    path = write_targets(
        tmp_path / "targets.csv",
        "name,ra,dec,epoch,tag\nStar,,,,named\n,10,-20,2000,coords\n,10,-20,2000,duplicate\n",
    )
    service = BatchService(session_factory, **factories(session_factory))
    created = service.create(path, refresh=("simbad", "2mass"))
    summary = service.execute(created.run_id)
    assert summary.status == "completed"
    assert summary.item_count == 3
    assert summary.job_counts == {"no_match": 6, "succeeded": 3}
    with session_factory() as session:
        assert session.query(Target).count() == 1
        assert session.query(ImportItem).count() == 3
        assert session.query(ImportJob).count() == 9
        assert all(item.target_id is not None for item in session.scalars(select(ImportItem)))


def test_identity_stage_batches_simbad_name_resolution(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "name\nStar A\nStar B\n")
    simbad = FakeBulkSimbad({
        "Star A": simbad_result("Star A", astrometry(10, -20, source="simbad")),
        "Star B": simbad_result("Star B", astrometry(11, -21, source="simbad")),
    })
    service = BatchService(
        session_factory,
        identity_factory=lambda: IdentityService(
            session_factory,
            simbad=simbad,
            gaia=FakeGaia(),
        ),
        metadata_factory=lambda: MetadataService(session_factory, None),
        catalog_factory=lambda: CatalogAcquisitionService(session_factory, {}),
    )
    created = service.create(path)
    summary = service.execute(created.run_id)
    assert summary.status == "completed"
    assert summary.job_counts == {"succeeded": 2}
    assert simbad.bulk_calls == [("Star A", "Star B")]
    assert simbad.single_calls == []


def test_tsv_is_detected_and_extra_columns_are_preserved(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.tsv", "ra\tdec\tproject\n10\t-20\talpha\n")
    service = BatchService(session_factory, **factories(session_factory))
    created = service.create(path)
    service.execute(created.run_id)
    with session_factory() as session:
        item = session.scalars(select(ImportItem)).one()
        assert '"project": "alpha"' in item.input_json


def test_batch_accepts_explicit_degree_column_names(session_factory, tmp_path):
    path = write_targets(
        tmp_path / "targets.csv",
        "name,ra_deg,dec_deg\nUnknown source,10.5,-20.25\n",
    )
    service = BatchService(session_factory, **factories(session_factory))
    created = service.create(path)
    summary = service.execute(created.run_id)
    assert summary.job_counts == {"succeeded": 1}


def test_transient_failure_can_be_retried_and_resumed(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "ra,dec\n10,-20\n")
    failing = FakeMetadataProvider(error=ProviderError("timeout", transient=True))
    service = BatchService(
        session_factory,
        **factories(session_factory, metadata_provider=failing),
    )
    run = service.create(path, refresh=("simbad",))
    first = service.execute(run.run_id)
    assert first.status == "partial"
    assert first.job_counts["transient_failure"] == 1
    with session_factory() as session:
        failed_job = session.scalar(select(ImportJob).where(ImportJob.stage == "simbad"))
        assert failed_job.last_error == "timeout"
        assert failed_job.next_retry_at is not None

    service.metadata_factory = lambda: MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("no_match")),
    )
    assert service.retry(run.run_id, failures="transient") == 1
    second = service.execute(run.run_id)
    assert second.status == "completed"
    with session_factory() as session:
        metadata_job = session.scalar(select(ImportJob).where(ImportJob.stage == "simbad"))
        assert metadata_job.attempts == 2


def test_resume_recovers_jobs_left_running_by_interruption(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "ra,dec\n10,-20\n")
    service = BatchService(session_factory, **factories(session_factory))
    run = service.create(path)
    with session_factory.begin() as session:
        job = session.scalars(select(ImportJob)).one()
        job.status = "running"
    summary = service.execute(run.run_id)
    assert summary.status == "completed"
    assert summary.job_counts == {"succeeded": 1}


def test_invalid_input_is_recorded_and_downstream_jobs_are_skipped(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "name,ra,dec\n,,\n")
    service = BatchService(session_factory, **factories(session_factory))
    run = service.create(path, refresh=("simbad", "2mass"))
    summary = service.execute(run.run_id)
    assert summary.status == "partial"
    assert summary.job_counts == {"permanent_failure": 1, "skipped": 2}


def test_completed_resume_is_idempotent(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "ra,dec\n10,-20\n")
    service = BatchService(session_factory, **factories(session_factory))
    run = service.create(path)
    service.execute(run.run_id)
    again = service.execute(run.run_id)
    assert again.status == "completed"
    with session_factory() as session:
        job = session.scalars(select(ImportJob)).one()
        assert job.attempts == 1


def test_provider_client_construction_network_error_is_retryable(session_factory, tmp_path):
    from pyvo.dal.exceptions import DALServiceError

    path = write_targets(tmp_path / "targets.csv", "ra,dec\n10,-20\n")

    def broken_factory():
        raise DALServiceError("temporary capabilities failure")

    service = BatchService(
        session_factory,
        identity_factory=broken_factory,
        metadata_factory=lambda: MetadataService(session_factory, None),
        catalog_factory=lambda: CatalogAcquisitionService(session_factory, {}),
    )
    run = service.create(path)
    summary = service.execute(run.run_id)
    assert summary.job_counts == {"transient_failure": 1}


def test_allwise_is_supported_as_a_batch_stage(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "ra,dec\n10,-20\n")
    base = factories(session_factory)
    base["catalog_factory"] = lambda: CatalogAcquisitionService(
        session_factory,
        {"allwise": FakeCatalog([], name="allwise", release="fake-allwise", query_epoch=2010.3)},
    )
    service = BatchService(session_factory, **base)
    run = service.create(path, refresh=("allwise",))
    summary = service.execute(run.run_id)
    assert summary.status == "completed"
    assert summary.job_counts == {"no_match": 1, "succeeded": 1}


def test_gaia_photometry_is_supported_as_a_batch_stage(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "ra,dec\n10,-20\n")
    base = factories(session_factory)
    base["catalog_factory"] = lambda: CatalogAcquisitionService(
        session_factory,
        {
            "gaia_dr3": FakeCatalog(
                [], name="gaia_dr3", release="fake-gaia-dr3", query_epoch=2016.0
            )
        },
    )
    service = BatchService(session_factory, **base)
    run = service.create(path, refresh=("gaia_dr3",))
    summary = service.execute(run.run_id)
    assert summary.status == "completed"
    assert summary.job_counts == {"no_match": 1, "succeeded": 1}


def test_bulk_catalog_stage_uses_refresh_many(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "ra,dec\n10,-20\n11,-21\n")
    adapter = FakeBulkCatalog(
        [],
        name="gaia_dr3",
        release="fake-gaia-dr3",
        query_epoch=2016.0,
    )
    base = factories(session_factory)
    base["catalog_factory"] = lambda: CatalogAcquisitionService(
        session_factory,
        {"gaia_dr3": adapter},
    )
    service = BatchService(session_factory, **base)
    run = service.create(path, refresh=("gaia_dr3",))
    summary = service.execute(run.run_id)
    assert summary.status == "completed"
    assert summary.job_counts == {"succeeded": 4}
    assert len(adapter.batches) == 1
    assert len(adapter.batches[0]) == 2


def test_tycho2_is_supported_as_a_batch_stage(session_factory, tmp_path):
    path = write_targets(tmp_path / "targets.csv", "ra,dec\n10,-20\n")
    base = factories(session_factory)
    base["catalog_factory"] = lambda: CatalogAcquisitionService(
        session_factory,
        {
            "tycho2": FakeCatalog(
                [], name="tycho2", release="I/259", query_epoch=2000.0
            )
        },
    )
    service = BatchService(session_factory, **base)
    run = service.create(path, refresh=("tycho2",))
    summary = service.execute(run.run_id)
    assert summary.status == "completed"
    assert summary.job_counts == {"no_match": 1, "succeeded": 1}
