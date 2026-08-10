"""Small, portable description of joint-fit observation topology."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


JOINT_FIT_VERSION = 1


class _IndentedSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


@dataclass(frozen=True)
class JointFitDefinition:
    """Non-default mappings from observation IDs to physical model IDs.

    An observation omitted from ``observations`` contributes only to the model
    with the same ID.  This keeps ordinary single-target exports free of
    metadata and makes the file describe only genuinely joint observations.
    """

    observations: Mapping[str, tuple[str, ...]]
    version: int = JOINT_FIT_VERSION

    def __post_init__(self) -> None:
        if self.version != JOINT_FIT_VERSION:
            raise ValueError(
                f"unsupported joint-fit version: {self.version}"
            )
        if not isinstance(self.observations, Mapping):
            raise ValueError("joint-fit observations must be a mapping")
        normalized: dict[str, tuple[str, ...]] = {}
        for observation, contributors in self.observations.items():
            if not isinstance(observation, str) or not observation.strip():
                raise ValueError("observation IDs must be non-empty strings")
            if isinstance(contributors, str):
                raise ValueError(
                    f"contributors for {observation!r} must be a list"
                )
            try:
                values = tuple(contributors)
            except TypeError as error:
                raise ValueError(
                    f"contributors for {observation!r} must be a list"
                ) from error
            if not values:
                raise ValueError(
                    f"observation {observation!r} has no contributors"
                )
            if any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError("contributor IDs must be non-empty strings")
            if len(set(values)) != len(values):
                raise ValueError(
                    f"observation {observation!r} repeats a contributor"
                )
            normalized[observation] = values
        object.__setattr__(self, "observations", normalized)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "observations": {
                observation: list(contributors)
                for observation, contributors in sorted(
                    self.observations.items()
                )
            },
        }


def read_joint_fit(path: str | Path) -> JointFitDefinition:
    """Read and validate a hand-authored joint-fit YAML file."""

    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("joint-fit YAML must contain a mapping")
    unknown = set(payload) - {"version", "observations"}
    if unknown:
        raise ValueError(
            "unknown joint-fit keys: " + ", ".join(sorted(unknown))
        )
    observations = payload.get("observations")
    if not isinstance(observations, dict):
        raise ValueError("joint-fit observations must be a mapping")
    return JointFitDefinition(
        version=payload.get("version"),
        observations=observations,
    )


def write_joint_fit(
    path: str | Path,
    definition: JointFitDefinition,
) -> Path:
    """Atomically write the intentionally small YAML interchange format."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_value = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_value)
    try:
        temporary.write_text(
            yaml.dump(
                definition.as_dict(),
                Dumper=_IndentedSafeDumper,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path
