from __future__ import annotations

import pytest

from sdb_identity.identifiers import normalize_identifier
from sdb_identity.models.identity import ExternalIdentifier, Target
from sdb_identity.targets import (
    AmbiguousTargetReference,
    TargetRepository,
    resolve_target,
    resolve_targets,
)


def _target(session, sdbid: str, ra_deg: float) -> Target:
    value = Target(sdbid=sdbid, ra2000_deg=ra_deg, dec2000_deg=-20.0)
    session.add(value)
    session.flush()
    return value


def _alias(session, target: Target, value: str) -> None:
    session.add(ExternalIdentifier(
        target_id=target.id,
        value=value,
        normalized_value=normalize_identifier(value),
        source="test",
    ))


def test_identifier_normalization_is_shared_lookup_form():
    assert normalize_identifier("  hd   123 a ") == "HD 123 A"


def test_repository_resolves_id_sdbid_and_unique_alias(session_factory):
    with session_factory.begin() as session:
        target = _target(session, "sdbid-v3-test-a", 10.0)
        _alias(session, target, "HD 123")
        target_id = target.id

    with session_factory() as session:
        repository = TargetRepository(session)
        assert repository.resolve_one(target_id).id == target_id
        assert repository.resolve_one(str(target_id)).id == target_id
        assert repository.resolve_one("sdbid-v3-test-a").id == target_id
        assert repository.resolve_one("  hd 123 ").id == target_id
        assert repository.resolve_one("missing") is None


def test_shared_alias_requires_explicit_many_target_resolution(session_factory):
    with session_factory.begin() as session:
        first = _target(session, "sdbid-v3-test-a", 10.0)
        second = _target(session, "sdbid-v3-test-b", 11.0)
        _alias(session, first, "HD 123")
        _alias(session, second, "HD 123")

    with session_factory() as session:
        assert [
            target.sdbid for target in resolve_targets(session, "HD 123")
        ] == ["sdbid-v3-test-a", "sdbid-v3-test-b"]
        with pytest.raises(
            AmbiguousTargetReference,
            match=(
                "target reference is ambiguous: HD 123; matches "
                "sdbid-v3-test-a, sdbid-v3-test-b"
            ),
        ):
            resolve_target(session, "HD 123")


def test_exact_sdbid_precedes_an_external_alias_with_same_text(session_factory):
    with session_factory.begin() as session:
        canonical = _target(session, "sdbid-v3-test-a", 10.0)
        other = _target(session, "sdbid-v3-test-b", 11.0)
        _alias(session, other, canonical.sdbid)
        canonical_id = canonical.id

    with session_factory() as session:
        assert resolve_target(session, "sdbid-v3-test-a").id == canonical_id
