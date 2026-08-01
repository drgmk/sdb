"""Shared dependencies and process settings for CLI domain commands."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from .cli_output import format_json, provider_output_to_stderr
from .config import SdbConfig
from .progress import ProgressReporter


@dataclass(frozen=True)
class CliContext:
    args: argparse.Namespace
    config: SdbConfig
    reporter: ProgressReporter
    database_path: Path
    reference_database_path: Path
    cache_database_path: Path
    offline: bool
    sessions: sessionmaker[Session] | None = None

    @classmethod
    def from_args(
        cls,
        args: argparse.Namespace,
        reporter: ProgressReporter,
    ) -> "CliContext":
        return cls(
            args=args,
            config=args.sdb_config,
            reporter=reporter,
            database_path=Path(args.database),
            reference_database_path=Path(args.reference_database),
            cache_database_path=Path(args.cache_database),
            offline=bool(args.offline),
        )

    def with_sessions(
        self,
        sessions: sessionmaker[Session],
    ) -> "CliContext":
        return replace(self, sessions=sessions)

    def require_sessions(self) -> sessionmaker[Session]:
        if self.sessions is None:
            raise RuntimeError("this command requires the main SDB database")
        return self.sessions

    def json(self, value: object, **kwargs: object) -> str:
        return format_json(self.args, value, **kwargs)

    def provider_output(self):
        return provider_output_to_stderr()
