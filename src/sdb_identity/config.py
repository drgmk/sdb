"""Small, general SDB configuration loader.

Configuration is layered from the user file and then the project file.  An
explicit ``--config``/``SDB_CONFIG`` path replaces that discovery.  The shape
is intentionally not tied to one command so mirrors, operator defaults, and
future service settings can live alongside reference-snapshot policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from typing import Iterable


@dataclass(frozen=True)
class SdbConfig:
    values: dict[str, object]
    sources: tuple[Path, ...] = ()

    def section(self, name: str) -> dict[str, object]:
        value = self.values.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"configuration section [{name}] must be a table")
        return value

    def reference_providers(
        self, available: Iterable[str],
    ) -> tuple[str, ...]:
        available_values = tuple(available)
        configured = self.section("reference").get("providers", ["all"])
        if not isinstance(configured, list) or not all(
            isinstance(value, str) for value in configured
        ):
            raise ValueError("reference.providers must be an array of provider names")
        clean = tuple(dict.fromkeys(value.strip().lower() for value in configured))
        if not clean or clean == ("all",):
            return available_values
        if "all" in clean:
            raise ValueError("reference.providers may use 'all' only by itself")
        unknown = sorted(set(clean) - set(available_values))
        if unknown:
            raise ValueError(
                f"unknown reference provider(s) in configuration: {', '.join(unknown)}"
            )
        return clean

    def reference_max_age_days(self) -> float:
        value = self.section("reference").get("max_age_days", 30)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("reference.max_age_days must be a positive number")
        result = float(value)
        if result <= 0:
            raise ValueError("reference.max_age_days must be a positive number")
        return result

    def catalog_providers(
        self, available: Iterable[str],
    ) -> tuple[str, ...]:
        """Return providers expected to cover each catalogued target."""
        available_values = tuple(available)
        configured = self.section("catalog").get("providers", ["all"])
        if not isinstance(configured, list) or not all(
            isinstance(value, str) for value in configured
        ):
            raise ValueError("catalog.providers must be an array of provider names")
        clean = tuple(dict.fromkeys(value.strip().lower() for value in configured))
        if not clean or clean == ("all",):
            return available_values
        if "all" in clean:
            raise ValueError("catalog.providers may use 'all' only by itself")
        unknown = sorted(set(clean) - set(available_values))
        if unknown:
            raise ValueError(
                f"unknown catalog provider(s) in configuration: {', '.join(unknown)}"
            )
        return clean

    def apply_environment_defaults(self) -> None:
        mirrors = self.section("mirrors")
        operator = self.section("operator")
        environment = {
            "SDB_SIMBAD_SERVER": mirrors.get("simbad"),
            "SDB_VIZIER_SERVER": mirrors.get("vizier"),
            "SDB_ACTOR": operator.get("actor"),
        }
        for name, value in environment.items():
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} configuration value must be a non-empty string")
            os.environ.setdefault(name, value.strip())


def load_config(path: str | Path | None = None) -> SdbConfig:
    explicit = path or os.environ.get("SDB_CONFIG")
    if explicit:
        sources = (Path(explicit).expanduser(),)
        if not sources[0].is_file():
            raise ValueError(f"configuration file not found: {sources[0]}")
    else:
        candidates = (
            Path.home() / ".config" / "sdb" / "config.toml",
            Path.cwd() / "sdb.toml",
        )
        sources = tuple(value for value in candidates if value.is_file())

    values: dict[str, object] = {}
    for source in sources:
        try:
            with source.open("rb") as handle:
                loaded = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"cannot read configuration {source}: {error}") from error
        values = _merge(values, loaded)
    return SdbConfig(values=values, sources=sources)


def _merge(
    base: dict[str, object], incoming: dict[str, object],
) -> dict[str, object]:
    result = dict(base)
    for key, value in incoming.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _merge(existing, value)
        else:
            result[key] = value
    return result
