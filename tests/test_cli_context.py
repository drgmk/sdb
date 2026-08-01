from __future__ import annotations

import argparse

import pytest

from sdb_identity.cli_context import CliContext
from sdb_identity.config import SdbConfig
from sdb_identity.database import make_session_factory
from sdb_identity.progress import ProgressReporter


def test_cli_context_stages_database_sessions(tmp_path):
    args = argparse.Namespace(
        sdb_config=SdbConfig({}),
        database=str(tmp_path / "main.sqlite"),
        reference_database=str(tmp_path / "reference.sqlite"),
        cache_database=str(tmp_path / "cache.sqlite"),
        offline=True,
        compact_json=False,
        command="alma",
        alma_command="status",
    )
    context = CliContext.from_args(args, ProgressReporter(False))

    assert context.database_path == tmp_path / "main.sqlite"
    assert context.reference_database_path == tmp_path / "reference.sqlite"
    assert context.cache_database_path == tmp_path / "cache.sqlite"
    assert context.offline is True
    with pytest.raises(RuntimeError, match="requires the main SDB database"):
        context.require_sessions()

    sessions = make_session_factory(context.database_path)
    database_context = context.with_sessions(sessions)
    assert database_context.require_sessions() is sessions
    assert context.sessions is None
