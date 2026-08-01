from __future__ import annotations

from sdb_identity.review_import_commands import (
    review_catalog_coverage_command,
    review_relatives_command,
)
from tests.test_system_expansion import _root_with_metadata


def test_import_and_coverage_previews_do_not_require_http(session_factory):
    root = _root_with_metadata(session_factory)

    relatives = review_relatives_command(
        session_factory,
        None,
        {"target": root.sdbid},
        apply=False,
    )
    assert relatives["mode"] == "preview"
    assert relatives["has_changes"] is True
    assert relatives["human_summary"]["title"] == (
        "SIMBAD-relative changes ready"
    )

    coverage = review_catalog_coverage_command(
        session_factory,
        ("2mass",),
        None,
        None,
        {"target": root.sdbid},
        apply=False,
    )
    assert coverage["mode"] == "preview"
    assert coverage["expected_providers"] == ["2mass"]
    assert coverage["missing_count"] >= 1
    assert coverage["action_available"] is False
    assert coverage["human_summary"]["warnings"] == [
        "This server cannot query missing remote providers in offline mode."
    ]
