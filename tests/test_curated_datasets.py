from __future__ import annotations

from astropy.table import Table
from sqlalchemy import select

from sdb_identity.datasets import CuratedDatasetService
from sdb_identity.dirty import pending_export_targets
from sdb_identity.export import export_ipac
from sdb_identity.models import CatalogRun, DatasetDirtyTarget, ExternalIdentifier
from sdb_identity.service import AddRequest, IdentityService, normalize_identifier


COLUMNS = (
    "record_no", "id", "hd", "name", "wav", "instrument", "fnu_mjy",
    "err_mjy", "sig3lim", "ref", "exclude",
)


def write_catalog(path, rows):
    table = Table(rows=rows, names=COLUMNS)
    table.write(path, format="ascii.ipac", overwrite=True)


def add_alias(session_factory, target_id, value):
    with session_factory() as session, session.begin():
        session.add(ExternalIdentifier(
            target_id=target_id,
            value=value,
            normalized_value=normalize_identifier(value),
            source="test",
        ))


def test_submm_import_associates_and_flows_to_export(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    add_alias(session_factory, target.target_id, "HD 1")
    source = tmp_path / "submm_obs.txt"
    write_catalog(source, [
        (1, "HD 1", "1", "Test", 850, "SCUBA-2", 12.5, 1.2, "0", "2020A&A...1A", "0"),
        (2, "Unknown", "", "", 1300, "ALMA", 0.5, "", "1", "2021A&A...2B", "0"),
    ])

    result = CuratedDatasetService(session_factory).import_submm_obs(source)
    assert (result.rows, result.matched, result.unresolved) == (2, 1, 1)
    assert (result.new, result.changed, result.removed, result.affected_targets) == (2, 0, 0, 1)

    output = export_ipac(session_factory, target.sdbid, tmp_path / "phot.txt")
    exported = Table.read(output, format="ascii.ipac")
    assert list(exported["Band"]) == ["WAV850"]
    assert list(exported["Phot"]) == [12.5]
    assert list(exported["Err"]) == [1.2]
    assert list(exported["Unit"]) == ["mJy"]
    assert list(exported["Note1"]) == ["Instr:SCUBA-2"]

    unresolved = CuratedDatasetService(session_factory).unresolved()
    assert [(row.record_no, row.source_identifier) for row in unresolved] == [(2, "Unknown")]


def test_submm_reimport_is_idempotent_and_tracks_changes(session_factory, tmp_path):
    first = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    second = IdentityService(session_factory).add(AddRequest(ra_deg=20, dec_deg=-30))
    add_alias(session_factory, first.target_id, "HD 1")
    add_alias(session_factory, second.target_id, "HD 2")
    source = tmp_path / "submm_obs.txt"
    write_catalog(source, [
        (1, "HD 1", "1", "", 850, "SCUBA", 10.0, 1.0, "0", "ref1", "0"),
        (2, "HD 2", "2", "", 1300, "ALMA", 2.0, 0.2, "0", "ref2", "0"),
    ])
    service = CuratedDatasetService(session_factory)
    initial = service.import_submm_obs(source)
    repeated = service.import_submm_obs(source)
    assert repeated.revision_id == initial.revision_id
    assert repeated.unchanged_revision is True

    write_catalog(source, [
        (1, "HD 1", "1", "", 850, "SCUBA", 11.0, 1.0, "0", "ref1", "0"),
    ])
    changed = service.import_submm_obs(source)
    assert (changed.new, changed.changed, changed.removed) == (0, 1, 1)
    assert changed.affected_targets == 2

    with session_factory() as session:
        current_runs = list(session.scalars(
            select(CatalogRun).where(
                CatalogRun.provider == "submm_obs", CatalogRun.is_current.is_(True)
            )
        ))
        assert [run.target_id for run in current_runs] == [first.target_id]
        dirty = list(session.scalars(
            select(DatasetDirtyTarget).where(DatasetDirtyTarget.revision_id == changed.revision_id)
        ))
        assert {row.target_id for row in dirty} == {first.target_id, second.target_id}

    first_output = Table.read(
        export_ipac(session_factory, first.sdbid, tmp_path / "first.txt"),
        format="ascii.ipac",
    )
    second_output = Table.read(
        export_ipac(session_factory, second.sdbid, tmp_path / "second.txt"),
        format="ascii.ipac",
    )
    assert list(first_output["Phot"]) == [11.0]
    assert len(second_output) == 0


def test_submm_import_rejects_duplicate_record_numbers(session_factory, tmp_path):
    source = tmp_path / "bad.txt"
    write_catalog(source, [
        (1, "HD 1", "", "", 850, "SCUBA", 1.0, 0.1, "0", "ref", "0"),
        (1, "HD 2", "", "", 850, "SCUBA", 1.0, 0.1, "0", "ref", "0"),
    ])
    try:
        CuratedDatasetService(session_factory).import_submm_obs(source)
    except ValueError as error:
        assert "duplicate record_no" in str(error)
    else:
        raise AssertionError("duplicate record_no was accepted")


def test_reconcile_matches_rows_after_target_alias_is_added(session_factory, tmp_path):
    source = tmp_path / "submm_obs.txt"
    write_catalog(source, [
        (1, "HD 99", "99", "", 850, "SCUBA", 4.0, 0.4, "0", "ref", "0"),
    ])
    service = CuratedDatasetService(session_factory)
    imported = service.import_submm_obs(source)
    assert imported.unresolved == 1

    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    add_alias(session_factory, target.target_id, "HD 99")
    reconciled = service.reconcile()
    assert (reconciled.newly_matched, reconciled.unresolved) == (1, 0)

    exported = Table.read(
        export_ipac(session_factory, target.sdbid, tmp_path / "reconciled.txt"),
        format="ascii.ipac",
    )
    assert list(exported["Phot"]) == [4.0]


def test_manual_associations_survive_reimport_and_can_be_removed(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    source = tmp_path / "submm_obs.txt"
    write_catalog(source, [
        (1, "Unknown", "", "", 850, "SCUBA", 4.0, 0.4, "0", "ref", "0"),
    ])
    service = CuratedDatasetService(session_factory)
    service.import_submm_obs(source)
    action = service.associate(
        "submm_obs", 1, target.sdbid, actor="tester", reason="identified from paper",
    )
    assert action.target_id == target.target_id

    write_catalog(source, [
        (1, "Corrected name", "", "", 850, "SCUBA", 4.5, 0.4, "0", "ref", "0"),
    ])
    imported = service.import_submm_obs(source)
    assert (imported.matched, imported.unresolved) == (1, 0)
    exported = Table.read(
        export_ipac(session_factory, target.sdbid, tmp_path / "manual.txt"),
        format="ascii.ipac",
    )
    assert list(exported["Phot"]) == [4.5]

    service.unassociate("submm_obs", 1, actor="tester", reason="wrong component")
    exported = Table.read(
        export_ipac(session_factory, target.sdbid, tmp_path / "unassociated.txt"),
        format="ascii.ipac",
    )
    assert len(exported) == 0
    assert service.unresolved()[0].association_method == "manual_unassociated"


def test_record_override_is_granular_and_export_clears_pending(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    add_alias(session_factory, target.target_id, "HD 1")
    source = tmp_path / "submm_obs.txt"
    write_catalog(source, [
        (1, "HD 1", "", "first", 850, "SCUBA", 4.0, 0.4, "0", "ref1", "0"),
        (2, "HD 1", "", "second", 850, "SCUBA", 5.0, 0.5, "0", "ref2", "0"),
    ])
    service = CuratedDatasetService(session_factory)
    service.import_submm_obs(source)
    assert len(service.pending()) == 1

    service.set_record_override(
        "submm_obs", 1, excluded=True, actor="tester", reason="contaminated",
    )
    exported = Table.read(
        export_ipac(session_factory, target.sdbid, tmp_path / "override.txt"),
        format="ascii.ipac",
    )
    assert list(exported["Phot"]) == [4.0, 5.0]
    assert list(exported["exclude"]) == [1, 0]
    assert "Override:contaminated" in exported[0]["Note2"]
    assert service.pending() == []

    service.set_record_override(
        "submm_obs", 1, excluded=False, actor="tester", reason="checked image",
    )
    assert len(service.pending()) == 1
    assert len(pending_export_targets(session_factory)) == 1
    service.mark_exported("submm_obs", target.sdbid)
    assert pending_export_targets(session_factory) == []
