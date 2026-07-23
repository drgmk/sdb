from __future__ import annotations

import warnings

import pytest

from sdb_identity.astrometry import make_sdbid, propagate_to_epoch
from tests.fakes import astrometry


@pytest.mark.parametrize(
    ("ra", "dec", "expected"),
    [
        (0.0, 0.0, "sdbid-v3-000000.00+000000.0"),
        (359.999999, -0.00001, "sdbid-v3-000000.00-000000.0"),
        (279.23475, 37.7836944, "sdbid-v3-183656.34+374701.3"),
        (15.0, -89.999, "sdbid-v3-010000.00-895956.4"),
    ],
)
def test_sdbid_format_and_rounding(ra, dec, expected):
    assert make_sdbid(ra, dec) == expected


def test_high_proper_motion_is_propagated_to_2000():
    value = astrometry(10.0, 20.0, epoch=2016.0, pmra=1000.0, pmdec=-500.0)
    propagated = propagate_to_epoch(value, 2000.0)
    assert propagated.ra_deg < 10.0
    assert propagated.dec_deg > 20.0
    assert propagated.epoch == 2000.0


def test_radial_velocity_is_stored_but_does_not_change_propagation():
    without_rv = astrometry(10.0, 20.0, epoch=2016.0, pmra=500.0, pmdec=200.0)
    with_rv = astrometry(10.0, 20.0, epoch=2016.0, pmra=500.0, pmdec=200.0, rv=100.0)
    a = propagate_to_epoch(without_rv, 2000.0)
    b = propagate_to_epoch(with_rv, 2000.0)
    assert b.radial_velocity_kms == 100.0
    assert b.ra_deg == pytest.approx(a.ra_deg, abs=1e-12)
    assert b.dec_deg == pytest.approx(a.dec_deg, abs=1e-12)


def test_unknown_distance_warning_is_suppressed_for_linear_pm_propagation():
    value = astrometry(10.0, 20.0, epoch=2016.0, pmra=500.0, pmdec=200.0)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        propagate_to_epoch(value, 2000.0)
    messages = [str(item.message) for item in caught]
    assert not any("distance overridden" in message for message in messages)


def test_missing_proper_motion_is_explicit():
    value = astrometry(10.0, 20.0, epoch=2016.0)
    propagated = propagate_to_epoch(value, 2000.0)
    assert propagated.ra_deg == 10.0
    assert propagated.dec_deg == 20.0
    assert propagated.proper_motion_available is False
