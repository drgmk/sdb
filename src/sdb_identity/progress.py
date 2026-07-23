from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ProgressReporter:
    """Small user-facing progress wrapper for long-running CLI loops.

    By default progress is interactive-only: bars are written to stderr when
    stderr is a TTY, and are otherwise disabled so JSON/stdout consumers and
    tests remain clean. CLI callers may force progress when auto-detection is
    too conservative, for example through `conda run`.
    """

    enabled: bool = False

    @classmethod
    def for_cli(cls, *, quiet: bool = False, force: bool = False) -> "ProgressReporter":
        return cls(enabled=not quiet and (force or sys.stderr.isatty()))

    def iter(
        self,
        values: Iterable[T],
        *,
        desc: str,
        total: int | None = None,
        unit: str = "it",
    ) -> Iterator[T]:
        if total == 0:
            yield from values
            return
        if not self.enabled:
            yield from values
            return
        try:
            from tqdm import tqdm
        except ImportError:
            yield from values
            return
        yield from tqdm(
            values,
            desc=desc,
            total=total,
            unit=unit,
            leave=True,
            file=sys.stderr,
        )

    def step(self, message: str) -> None:
        if self.enabled:
            sys.stderr.write(f"{message}\n")
            sys.stderr.flush()


NULL_PROGRESS = ProgressReporter(False)
