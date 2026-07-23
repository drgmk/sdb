from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def _url(path: str | Path) -> str:
    return f"sqlite:///{Path(path).expanduser().resolve()}"


def make_engine(path: str | Path) -> Engine:
    engine = create_engine(_url(path), future=True)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def make_session_factory(path: str | Path) -> sessionmaker[Session]:
    return sessionmaker(make_engine(path), expire_on_commit=False, future=True)


def init_database(path: str | Path, revision: str = "head") -> None:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", _url(path))
    command.upgrade(config, revision)
