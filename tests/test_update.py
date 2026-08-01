from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.metadata import MetadataQueryResult, MetadataService
from sdb_identity.models.identity import ExternalIdentifier
from sdb_identity.reference import ReferenceStore
from sdb_identity.samples import SampleService
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.update import UpdateService
from tests.test_catalog import FakeBulkCatalog, FakeCatalog
from tests.test_metadata import (
    FakeBulkMetadataProvider,
    FakeMetadataProvider,
    snapshot,
)


def service(session_factory, tmp_path):
    return UpdateService(
        session_factory,
        ReferenceStore(tmp_path / "reference.sqlite"),
        metadata_factory=lambda: MetadataService(
            session_factory, FakeMetadataProvider(MetadataQueryResult("no_match"))
        ),
        catalog_factory=lambda: CatalogAcquisitionService(
            session_factory,
            {
                "2mass": FakeCatalog([]),
                "allwise": FakeCatalog(
                    [], name="allwise", release="fake-allwise", query_epoch=2010.3
                ),
            },
        ),
        workers=2,
    )


def test_update_target_refreshes_absent_results_then_skips_current(
    session_factory, tmp_path
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    updater = service(session_factory, tmp_path)
    first = updater.update_target(target.sdbid, providers=("simbad", "2mass"))
    assert (first.refreshed, first.skipped, first.failed) == (2, 0, 0)
    assert {item.status for item in first.items} == {"no_match"}
    second = updater.update_target(target.sdbid, providers=("simbad", "2mass"))
    assert (second.refreshed, second.skipped) == (0, 2)


def test_update_reports_missing_snapshot_without_downloading_it(
    session_factory, tmp_path
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    result = service(session_factory, tmp_path).update_target(
        target.sdbid, providers=("hip2",)
    )
    assert (result.missing, result.failed) == (1, 0)
    assert "reference fetch hip2" in result.items[0].detail


def test_update_all_runs_remote_jobs_for_each_target(session_factory, tmp_path):
    IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    IdentityService(session_factory).add(AddRequest(ra_deg=20, dec_deg=-20))
    result = service(session_factory, tmp_path).update_all(providers=("2mass",))
    assert (result.target_count, result.refreshed, result.failed) == (2, 2, 0)


def test_update_explicit_sample_members_only(session_factory, tmp_path):
    first = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    IdentityService(session_factory).add(AddRequest(ra_deg=20, dec_deg=-20))
    samples = SampleService(session_factory)
    samples.create("selected")
    samples.add("selected", first.target_id, actor="grant", reason="test selection")

    members = samples.members("selected")
    result = service(session_factory, tmp_path).update_targets(
        [target.id for target in members], providers=("2mass",),
    )

    assert (result.target_count, result.refreshed, result.failed) == (1, 1, 0)
    assert {item.target_id for item in result.items} == {first.target_id}


def test_update_all_uses_bulk_capable_catalog_adapter(session_factory, tmp_path):
    IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    IdentityService(session_factory).add(AddRequest(ra_deg=20, dec_deg=-20))
    adapter = FakeBulkCatalog([])
    updater = UpdateService(
        session_factory,
        ReferenceStore(tmp_path / "reference.sqlite"),
        metadata_factory=lambda: MetadataService(
            session_factory, FakeMetadataProvider(MetadataQueryResult("no_match"))
        ),
        catalog_factory=lambda: CatalogAcquisitionService(
            session_factory, {"2mass": adapter}
        ),
        workers=2,
    )

    result = updater.update_all(providers=("2mass",))

    assert (result.target_count, result.refreshed, result.failed) == (2, 2, 0)
    assert len(adapter.batches) == 1
    assert adapter.contexts == []


def test_update_all_uses_bulk_capable_simbad_metadata(session_factory, tmp_path):
    IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    IdentityService(session_factory).add(AddRequest(ra_deg=20, dec_deg=-20))
    provider = FakeBulkMetadataProvider(MetadataQueryResult("no_match"))
    updater = UpdateService(
        session_factory,
        ReferenceStore(tmp_path / "reference.sqlite"),
        metadata_factory=lambda: MetadataService(session_factory, provider),
        catalog_factory=lambda: CatalogAcquisitionService(session_factory, {}),
        workers=2,
    )

    first = updater.update_all(providers=("simbad",))
    second = updater.update_all(providers=("simbad",))

    assert (first.target_count, first.refreshed, first.skipped, first.failed) == (2, 2, 0, 0)
    assert (second.refreshed, second.skipped, second.failed) == (0, 2, 0)
    assert [context.target_id for context in provider.contexts] == [1, 2]


def test_update_all_stores_simbad_aliases_before_applying_snapshots(
    session_factory,
    tmp_path,
    monkeypatch,
):
    IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    updater = UpdateService(
        session_factory,
        ReferenceStore(tmp_path / "reference.sqlite"),
        metadata_factory=lambda: MetadataService(
            session_factory,
            FakeMetadataProvider(MetadataQueryResult("match", (snapshot(),))),
        ),
        catalog_factory=lambda: CatalogAcquisitionService(session_factory, {}),
        workers=2,
    )
    monkeypatch.setattr(
        updater.reference_store, "current_snapshot", lambda _provider: object()
    )
    observed_aliases = []

    def apply_after_metadata(_service, provider, *, force=False):
        with session_factory() as session:
            observed_aliases.extend(session.scalars(
                select(ExternalIdentifier.value).where(
                    ExternalIdentifier.source == "simbad_metadata"
                )
            ))
        return SimpleNamespace(
            unchanged=False,
            targets=1,
            refreshed=1,
        )

    monkeypatch.setattr(
        "sdb_identity.update.ReferenceApplicationService.apply",
        apply_after_metadata,
    )

    result = updater.update_all(providers=("hip2", "simbad"))

    assert result.failed == 0
    assert "TYC 1-2-3" in observed_aliases
