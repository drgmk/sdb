from __future__ import annotations

from astropy.table import Table
from sqlalchemy import select

from sdb_identity.catalogs import CatalogService
from sdb_identity.dirty import pending_export_targets
from sdb_identity.export import export_ipac
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
    service = CatalogService(session_factory, {"2mass": adapter})
    service.refresh(target.sdbid, "2mass")
    assert len(pending_export_targets(session_factory)) == 1
    export_ipac(session_factory, target.sdbid, tmp_path / "with-photometry.txt")
    service.refresh(target.sdbid, "2mass")
    assert pending_export_targets(session_factory) == []


def test_export_dirty_output_remains_sdf_readable(session_factory, tmp_path):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    output = export_ipac(session_factory, target.sdbid, tmp_path / "target.txt")
    assert Table.read(output, format="ascii.ipac").meta["keywords"]["id"]["value"] == target.sdbid
