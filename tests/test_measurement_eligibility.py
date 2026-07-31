from __future__ import annotations

from sqlalchemy import select

from sdb_identity.catalogs import CatalogService
from sdb_identity.measurement_eligibility import (
    effective_measurement_eligibility,
)
from sdb_identity.models import NormalizedMeasurement
from sdb_identity.photometry import set_measurement_eligibility
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog, candidate, measurement


def _catalog(source_id: str, value: float = 8.1) -> FakeCatalog:
    return FakeCatalog(
        [candidate(
            source_id,
            measurements=[measurement("WISE3P4", value)],
        )],
        name="allwise",
        release="eligibility-test",
    )


def test_same_provider_and_band_measurements_are_decided_independently(
    session_factory,
):
    identity = IdentityService(session_factory)
    first = identity.add(AddRequest(ra_deg=10, dec_deg=-20))
    second = identity.add(AddRequest(ra_deg=20, dec_deg=-30))
    service = CatalogService(session_factory, {
        "allwise": _catalog("first"),
    })
    service.refresh(first.sdbid, "allwise")
    service.adapters["allwise"] = _catalog("second", 9.2)
    service.refresh(second.sdbid, "allwise")
    with session_factory() as session:
        rows = list(session.scalars(
            select(NormalizedMeasurement).order_by(
                NormalizedMeasurement.id
            )
        ))

    set_measurement_eligibility(
        session_factory,
        rows[0].id,
        excluded=True,
        actor="reviewer",
        reason="first source is contaminated",
    )
    with session_factory() as session:
        eligibility = effective_measurement_eligibility(
            session, [row.id for row in rows],
        )

    assert eligibility[rows[0].id].excluded is True
    assert eligibility[rows[0].id].basis == "manual_exclude_action"
    assert eligibility[rows[1].id].excluded is False
    assert eligibility[rows[1].id].basis == "included"


def test_manual_include_overrides_shared_detection_safety(session_factory):
    identity = IdentityService(session_factory)
    first = identity.add(AddRequest(ra_deg=10, dec_deg=-20))
    second = identity.add(AddRequest(ra_deg=10.0002, dec_deg=-20))
    service = CatalogService(session_factory, {
        "allwise": _catalog("shared"),
    })
    service.refresh(first.sdbid, "allwise")
    service.refresh(second.sdbid, "allwise")
    with session_factory() as session:
        measurement_id = session.scalar(select(NormalizedMeasurement.id))
        automatic = effective_measurement_eligibility(
            session, [measurement_id],
        )[measurement_id]
    assert automatic.excluded is True
    assert automatic.basis == "shared_detection"

    set_measurement_eligibility(
        session_factory,
        measurement_id,
        excluded=False,
        actor="reviewer",
        reason="combined-light fit intentionally uses this source",
    )
    with session_factory() as session:
        manual = effective_measurement_eligibility(
            session, [measurement_id],
        )[measurement_id]
    assert manual.excluded is False
    assert manual.basis == "manual_include_action"
