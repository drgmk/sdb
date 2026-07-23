from __future__ import annotations

from sdb_identity.progress import ProgressReporter


def test_progress_auto_detection_and_overrides(monkeypatch):
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)

    assert not ProgressReporter.for_cli().enabled
    assert ProgressReporter.for_cli(force=True).enabled
    assert not ProgressReporter.for_cli(quiet=True, force=True).enabled

    monkeypatch.setattr("sys.stderr.isatty", lambda: True)

    assert ProgressReporter.for_cli().enabled
