import pytest

from sdb_identity.photometry.semantics import validate_photometry_semantics


@pytest.mark.parametrize(
    ("scope", "state"),
    [
        ("component", "clear"),
        ("system", "blended"),
        ("shared", "unknown"),
        ("ambiguous", "ambiguous"),
    ],
)
def test_canonical_photometry_semantics(scope, state):
    assert validate_photometry_semantics(scope, state) == (scope, state)


@pytest.mark.parametrize(
    ("scope", "state"),
    [
        ("blended", "clear"),
        ("component", "provider_flagged"),
        ("component", "likely_blended_at_catalog_resolution"),
    ],
)
def test_photometry_semantics_rejects_evidence_as_state(scope, state):
    with pytest.raises(ValueError):
        validate_photometry_semantics(scope, state)
