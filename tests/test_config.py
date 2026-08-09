from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import os

from sdb_identity.cli import main
from sdb_identity.config import load_config
from sdb_identity.reference.ensure import ensure_reference_snapshots


def test_config_layers_reference_defaults_and_mirrors(tmp_path, monkeypatch):
    config = tmp_path / "sdb.toml"
    config.write_text(
        "[reference]\n"
        'providers = ["hip2", "tdsc"]\n'
        "max_age_days = 45\n"
        "[mirrors]\n"
        'simbad = "simbad.example"\n'
        'vizier = "vizier.example"\n'
        "[operator]\n"
        'actor = "configured reviewer"\n'
    )
    for name in ("SDB_SIMBAD_SERVER", "SDB_VIZIER_SERVER", "SDB_ACTOR"):
        monkeypatch.delenv(name, raising=False)

    try:
        value = load_config(config)
        value.apply_environment_defaults()

        assert value.reference_providers(("hip2", "tdsc", "koen10")) == (
            "hip2", "tdsc",
        )
        assert value.reference_max_age_days() == 45
        assert value.catalog_providers(("2mass", "tycho2")) == (
            "2mass", "tycho2",
        )
        assert value.sources == (config,)
        assert os.environ["SDB_SIMBAD_SERVER"] == "simbad.example"
        assert os.environ["SDB_VIZIER_SERVER"] == "vizier.example"
        assert os.environ["SDB_ACTOR"] == "configured reviewer"
    finally:
        for name in ("SDB_SIMBAD_SERVER", "SDB_VIZIER_SERVER", "SDB_ACTOR"):
            os.environ.pop(name, None)


def test_reference_provider_default_expands_to_all(tmp_path):
    config = tmp_path / "empty.toml"
    config.write_text("")
    assert load_config(config).reference_providers(("hip2", "tdsc")) == (
        "hip2", "tdsc",
    )
    assert load_config(config).catalog_providers(("2mass", "tycho2")) == (
        "2mass", "tycho2",
    )


def test_catalog_provider_configuration_can_select_a_subset(tmp_path):
    config = tmp_path / "catalog.toml"
    config.write_text(
        "[catalog]\n"
        'providers = ["2mass", "tycho2"]\n'
    )
    assert load_config(config).catalog_providers(
        ("gaia_dr3", "tycho2", "2mass")
    ) == ("2mass", "tycho2")


def test_export_root_can_come_from_config_or_environment(tmp_path, monkeypatch):
    config = tmp_path / "export.toml"
    configured = tmp_path / "configured"
    overridden = tmp_path / "overridden"
    config.write_text(f'[export]\nroot = "{configured}"\n')

    value = load_config(config)
    assert value.export_root() == configured
    monkeypatch.setenv("SDB_EXPORT_ROOT", str(overridden))
    assert value.export_root() == overridden


def test_reference_ensure_fetches_only_missing_and_stale():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)

    class FakeStore:
        def __init__(self):
            self.snapshots = {
                "current": SimpleNamespace(
                    id=1, retrieved_at=now - timedelta(days=2),
                ),
                "stale": SimpleNamespace(
                    id=2, retrieved_at=now - timedelta(days=60),
                ),
            }
            self.fetches = []

        def current_snapshot(self, provider):
            return self.snapshots.get(provider)

        def fetch(self, provider, *, cache_path, refresh_cache, reporter):
            self.fetches.append((provider, cache_path, refresh_cache))
            return SimpleNamespace(
                snapshot_id=10 + len(self.fetches),
                content_sha256=f"hash-{provider}",
                row_count=100,
                unchanged=False,
            )

    store = FakeStore()
    messages = []
    reporter = SimpleNamespace(step=messages.append)
    result = ensure_reference_snapshots(
        store,
        ("current", "missing", "stale"),
        cache_path="cache.sqlite",
        max_age_days=30,
        now=now,
        reporter=reporter,
    )

    assert result["summary"] == {
        "current": 1,
        "missing": 1,
        "stale": 1,
        "fetched": 2,
        "checked": 0,
    }
    assert store.fetches == [
        ("missing", "cache.sqlite", False),
        ("stale", "cache.sqlite", True),
    ]
    assert messages == [
        "current: current",
        "missing: missing; fetching reference snapshot",
        "missing: fetched 100 reference rows",
        "stale: stale; fetching reference snapshot",
        "stale: fetched 100 reference rows",
    ]


def test_reference_ensure_cli_uses_configured_provider_list(
    tmp_path, capsys,
):
    config = tmp_path / "sdb.toml"
    config.write_text(
        "[reference]\n"
        'providers = ["hip2", "tdsc"]\n'
        "max_age_days = 90\n"
    )
    assert main([
        "--config", str(config),
        "--reference-database", str(tmp_path / "reference.sqlite"),
        "reference", "ensure", "--check",
    ]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["max_age_days"] == 90
    assert [row["provider"] for row in value["providers"]] == [
        "hip2", "tdsc",
    ]
