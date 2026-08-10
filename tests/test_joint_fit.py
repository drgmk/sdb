from __future__ import annotations

import pytest

from sdb_identity.joint_fit import (
    JointFitDefinition,
    read_joint_fit,
    write_joint_fit,
)


def test_joint_fit_yaml_is_small_and_round_trips(tmp_path):
    path = tmp_path / "joint-fit.yml"
    definition = JointFitDefinition(observations={
        "target123AB": ("target123A", "target123B"),
    })

    write_joint_fit(path, definition)

    assert path.read_text() == (
        "version: 1\n"
        "observations:\n"
        "  target123AB:\n"
        "    - target123A\n"
        "    - target123B\n"
    )
    assert read_joint_fit(path) == definition


@pytest.mark.parametrize("content", [
    "version: 2\nobservations: {}\n",
    "version: 1\nobservations:\n  AB: A\n",
    "version: 1\nobservations:\n  AB: []\n",
    "version: 1\nobservations:\n  AB: [A, A]\n",
    "version: 1\nobservations: {}\nextra: true\n",
])
def test_joint_fit_yaml_rejects_invalid_contract(tmp_path, content):
    path = tmp_path / "joint-fit.yml"
    path.write_text(content)

    with pytest.raises(ValueError):
        read_joint_fit(path)
