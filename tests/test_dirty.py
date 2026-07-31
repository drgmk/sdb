from __future__ import annotations

import pytest
from astropy.table import Table
from sqlalchemy import select

from sdb_identity.catalog_acquisition import CatalogAcquisitionService
from sdb_identity.dirty import mark_export_dirty, pending_export_targets
from sdb_identity.export import (
    export_ipac,
    load_target_export_snapshot,
    serialize_ipac,
)
from sdb_identity.models import ExportDirtyTarget
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement


def test_target_creation_is_dirty_and_successful_export_clears_it(
    session_factory, tmp_path
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    pending = pending_export_targets(session_factory)
    assert [(value[0].id, value[1]) for value in pending] == [(target.target_id, 1)]
    export_ipac(session_factory, target.sdbid, tmp_path / "target.txt")
    assert pending_export_targets(session_factory) == []
    with session_factory() as session:
        event = session.scalar(select(ExportDirtyTarget))
        assert event.source_type == "identity"
        assert event.exported_at is not None


def test_catalog_refresh_marks_only_material_changes_dirty(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    export_ipac(session_factory, target.sdbid, tmp_path / "initial.txt")
    adapter = FakeCatalog([candidate(measurements=[measurement(value=7.1)])])
    service = CatalogAcquisitionService(session_factory, {"2mass": adapter})
    service.refresh(target.sdbid, "2mass")
    assert len(pending_export_targets(session_factory)) == 1
    export_ipac(session_factory, target.sdbid, tmp_path / "with-photometry.txt")
    service.refresh(target.sdbid, "2mass")
    assert pending_export_targets(session_factory) == []


def test_export_dirty_output_remains_sdf_readable(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    output = export_ipac(session_factory, target.sdbid, tmp_path / "target.txt")
    assert Table.read(output, format="ascii.ipac").meta["keywords"]["id"]["value"] == target.sdbid


def test_export_snapshot_is_pure_and_does_not_acknowledge_dirty_state(
    session_factory,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )

    with session_factory() as session:
        snapshot = load_target_export_snapshot(session, target.sdbid)
    table = serialize_ipac(snapshot.projection)

    assert snapshot.projection.sdbid == target.sdbid
    assert snapshot.dirty_event_watermark is not None
    assert table.meta["keywords"]["id"]["value"] == target.sdbid
    assert pending_export_targets(session_factory)[0][1] == 1


def test_export_acknowledges_only_events_in_its_read_snapshot(
    session_factory, tmp_path, monkeypatch,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    from sdb_identity import export as export_module

    original_write = export_module.write_ipac_atomic

    def write_then_change(projection, output):
        written = original_write(projection, output)
        with session_factory.begin() as session:
            mark_export_dirty(
                session,
                target.target_id,
                source_type="concurrent_test",
                source_id="newer",
                reason="change committed while an older snapshot was exporting",
            )
        return written

    monkeypatch.setattr(export_module, "write_ipac_atomic", write_then_change)
    export_ipac(session_factory, target.sdbid, tmp_path / "target.txt")

    with session_factory() as session:
        events = list(session.scalars(
            select(ExportDirtyTarget).order_by(ExportDirtyTarget.id)
        ))
    assert len(events) == 2
    assert events[0].exported_at is not None
    assert events[1].exported_at is None
    assert [(row[0].id, row[1]) for row in pending_export_targets(
        session_factory
    )] == [(target.target_id, 1)]


def test_export_watermark_uses_same_sqlite_snapshot_as_projection(
    session_factory, tmp_path, monkeypatch,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    from sdb_identity import export as export_module

    original_watermark = export_module.export_dirty_watermark
    inserted = False

    def commit_change_before_watermark(session, target_id):
        nonlocal inserted
        if not inserted:
            inserted = True
            with session_factory.begin() as concurrent:
                mark_export_dirty(
                    concurrent,
                    target.target_id,
                    source_type="snapshot_test",
                    source_id="between_reads",
                    reason="committed after projection reads began",
                )
        return original_watermark(session, target_id)

    monkeypatch.setattr(
        export_module,
        "export_dirty_watermark",
        commit_change_before_watermark,
    )
    export_ipac(session_factory, target.sdbid, tmp_path / "target.txt")

    with session_factory() as session:
        events = list(session.scalars(
            select(ExportDirtyTarget).order_by(ExportDirtyTarget.id)
        ))
    assert len(events) == 2
    assert events[0].exported_at is not None
    assert events[1].exported_at is None


def test_failed_atomic_export_preserves_destination_and_dirty_state(
    session_factory, tmp_path, monkeypatch,
):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    output = tmp_path / "target.txt"
    output.write_text("stable previous export\n", encoding="utf-8")

    def fail_after_partial_write(_table, temporary):
        temporary.write_text("partial replacement\n", encoding="utf-8")
        raise RuntimeError("synthetic serialization failure")

    monkeypatch.setattr(
        "sdb_identity.export._write_ipac_table",
        fail_after_partial_write,
    )
    with pytest.raises(RuntimeError, match="synthetic serialization failure"):
        export_ipac(session_factory, target.sdbid, output)

    assert output.read_text(encoding="utf-8") == "stable previous export\n"
    assert list(tmp_path.glob(".target.txt.*.tmp")) == []
    assert pending_export_targets(session_factory)[0][1] == 1
