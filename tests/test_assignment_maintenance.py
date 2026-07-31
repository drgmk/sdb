from __future__ import annotations

from sdb_identity.assignment_maintenance import (
    audit_automatic_assignment_sets,
)
from sdb_identity.catalog_measurements import current_measurement_encounters
from sdb_identity.catalogs import CatalogService, MeasurementValue
from sdb_identity.photometry import assign_measurement_target
from tests.test_catalog import FakeCatalog, candidate
from tests.test_system_photometry_foundation import _configured_system


def _measurement_for(session_factory, target, source_id):
    value = MeasurementValue(
        band="WISE22",
        value=5.1,
        error=0.05,
        systematic_error=0.02,
        unit="mag",
        bibcode="test",
    )
    CatalogService(session_factory, {
        "allwise": FakeCatalog(
            [candidate(source_id, measurements=[value])],
            name="allwise",
            release="test",
            query_epoch=2010.5,
        ),
    }).refresh(target.sdbid, "allwise")
    with session_factory() as session:
        return next(
            row.measurement
            for row in current_measurement_encounters(
                session, [target.target_id]
            )
            if row.measurement.source_id == source_id
        )


def test_audit_finds_only_complete_redundant_automatic_defaults(
    session_factory,
):
    system, component_a, component_b = _configured_system(session_factory)
    ordinary = _measurement_for(session_factory, component_a, "ordinary-wise")
    exception = _measurement_for(session_factory, system, "exception-wise")
    assign_measurement_target(
        session_factory,
        ordinary.id,
        component_a.sdbid,
        role="contributor",
        method="automatic_proposal",
        actor="test",
        reason="historical materialized default",
    )
    for target, role in (
        (system, "composite_scope"),
        (component_a, "contributor"),
        (component_b, "contributor"),
    ):
        assign_measurement_target(
            session_factory,
            exception.id,
            target.sdbid,
            role=role,
            method="automatic_proposal",
            actor="test",
            reason="shared-light exception",
        )

    with session_factory() as session:
        audit = audit_automatic_assignment_sets(session)

    assert audit.row_count == 4
    assert audit.measurement_count == 2
    assert audit.redundant_measurement_ids == (ordinary.id,)
    assert audit.redundant_row_count == 1
    assert audit.classification_counts == {
        "explicit_exception": 1,
        "redundant_default": 1,
    }
