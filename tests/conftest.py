from __future__ import annotations

import pytest

from sdb_identity.database import init_database, make_session_factory


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "sdb.sqlite"
    init_database(path)
    return path


@pytest.fixture
def session_factory(db_path):
    return make_session_factory(db_path)

