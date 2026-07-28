from __future__ import annotations

from dataclasses import dataclass
import json

import pytest
from sqlalchemy import select

from sdb_identity.catalogs import (
    CatalogCandidate,
    CatalogQueryContext,
    CatalogService,
    MeasurementValue,
)
from sdb_identity.models import CatalogBatchRequest, CatalogMatchOverride, CatalogRun, ExportDirtyTarget, ExternalIdentifier, NormalizedMeasurement, RawCatalogRow
from sdb_identity.adapters.allwise import AllWiseAdapter
from sdb_identity.providers import ProviderError
from sdb_identity.service import AddRequest, IdentityService


@dataclass
class FakeCatalog:
    candidates: list[CatalogCandidate]
    error: ProviderError | None = None
    name: str = "2mass"
    release: str = "fake-2mass"
    query_epoch: float = 1999.3

    def __post_init__(self):
        self.contexts: list[CatalogQueryContext] = []

    def query(self, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.candidates

    def normalize(self, candidate):
        return candidate.measurements


class FakeBulkCatalog(FakeCatalog):
    def __post_init__(self):
        super().__post_init__()
        self.batches = []
        self.bulk_error = None

    def query_many(self, contexts):
        self.batches.append(contexts)
        if self.bulk_error:
            raise self.bulk_error
        return {
            context.target_id: [candidate(
                source_id=f"bulk-{context.target_id}",
                ra=context.astrometry.ra_deg,
                dec=context.astrometry.dec_deg,
                measurements=[measurement()],
            )]
            for context in contexts
        }


def candidate(source_id="J00400000-2000000", *, ra=10.0, dec=-20.0, measurements=()):
    return CatalogCandidate(
        source_id=source_id,
        ra_deg=ra,
        dec_deg=dec,
        epoch=1999.3,
        payload={"2MASS": source_id, "RAJ2000": ra, "DEJ2000": dec},
        measurements=tuple(measurements),
    )


def measurement(band="2MJ", value=7.1, error=0.02, excluded=False):
    return MeasurementValue(
        band=band,
        value=value,
        error=error,
        systematic_error=0.01,
        unit="mag",
        bibcode="2003tmc..book.....C",
        quality="A0",
        excluded=excluded,
        exclusion_reason=None if not excluded else "2MASS quality/contamination flags",
        note1="Qflg:A",
        note2="Cflg:0",
    )


def add_target(session_factory, **kwargs):
    return IdentityService(session_factory).add(AddRequest(**kwargs))


def test_refresh_stores_raw_row_measurements_and_identifier(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=-20)
    adapter = FakeCatalog([candidate(measurements=[measurement(), measurement("2MH", 6.9)])])
    result = CatalogService(session_factory, {"2mass": adapter}).refresh(target.sdbid, "2mass")
    assert result.status == "match"
    assert result.measurement_count == 2
    with session_factory() as session:
        run = session.scalars(select(CatalogRun)).one()
        assert run.is_current is True
        assert session.query(RawCatalogRow).count() == 1
        assert session.query(NormalizedMeasurement).count() == 2
        identifier = session.scalar(select(ExternalIdentifier).where(ExternalIdentifier.source == "2mass"))
        assert identifier is None
        assert run.selected_source_id == "J00400000-2000000"


def test_refresh_fills_default_resolution_when_adapter_omits_it(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=-20)
    adapter = FakeCatalog(
        [candidate(measurements=[measurement("GAIA.G")])],
        name="gaia_dr3",
        release="fake-gaia",
        query_epoch=2016.0,
    )

    CatalogService(session_factory, {"gaia_dr3": adapter}).refresh(target.sdbid, "gaia_dr3")

    with session_factory() as session:
        value = session.scalars(select(NormalizedMeasurement)).one()

    assert value.resolution_major_arcsec is not None
    assert value.resolution_kind == "diffraction_estimate_1.22_lambda_over_d"
    assert "Gaia" in value.resolution_reference


def test_refresh_preserves_adapter_supplied_resolution(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=-20)
    explicit = MeasurementValue(
        band="GAIA.G",
        value=7.1,
        resolution_major_arcsec=9.0,
        resolution_minor_arcsec=8.0,
        resolution_kind="provider_test",
        resolution_reference="unit test",
    )
    adapter = FakeCatalog(
        [candidate(measurements=[explicit])],
        name="gaia_dr3",
        release="fake-gaia",
        query_epoch=2016.0,
    )

    CatalogService(session_factory, {"gaia_dr3": adapter}).refresh(target.sdbid, "gaia_dr3")

    with session_factory() as session:
        value = session.scalars(select(NormalizedMeasurement)).one()

    assert value.resolution_major_arcsec == 9.0
    assert value.resolution_minor_arcsec == 8.0
    assert value.resolution_kind == "provider_test"
    assert value.resolution_reference == "unit test"


def test_no_match_is_recorded_without_sentinel_measurements(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=-20)
    result = CatalogService(session_factory, {"2mass": FakeCatalog([])}).refresh(target.sdbid, "2mass")
    assert result.status == "no_match"
    with session_factory() as session:
        assert session.query(NormalizedMeasurement).count() == 0


def test_transient_failure_is_distinct_and_preserves_previous_current_run(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=-20)
    ok = CatalogService(session_factory, {"2mass": FakeCatalog([candidate(measurements=[measurement()])])})
    first = ok.refresh(target.sdbid, "2mass")
    failing = CatalogService(session_factory, {"2mass": FakeCatalog([], ProviderError("timeout", transient=True))})
    second = failing.refresh(target.sdbid, "2mass")
    assert second.status == "transient_failure"
    with session_factory() as session:
        current = session.scalars(select(CatalogRun).where(CatalogRun.is_current.is_(True))).all()
        assert [run.id for run in current] == [first.run_id]


def test_ambiguous_candidates_are_retained_without_measurements(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=-20)
    adapter = FakeCatalog([
        candidate("one", ra=10.00010, measurements=[measurement()]),
        candidate("two", ra=10.00011, measurements=[measurement()]),
    ])
    result = CatalogService(session_factory, {"2mass": adapter}).refresh(target.sdbid, "2mass")
    assert result.status == "ambiguous"
    with session_factory() as session:
        assert session.query(RawCatalogRow).count() == 2
        assert session.query(NormalizedMeasurement).count() == 0


def test_refresh_keeps_history_and_replaces_current_measurements(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=-20)
    service = CatalogService(session_factory, {"2mass": FakeCatalog([candidate(measurements=[measurement(value=7.1)])])})
    first = service.refresh(target.sdbid, "2mass")
    service.adapters["2mass"] = FakeCatalog([candidate(measurements=[measurement(value=7.2)])])
    second = service.refresh(target.sdbid, "2mass")
    with session_factory() as session:
        runs = session.scalars(select(CatalogRun).order_by(CatalogRun.id)).all()
        assert [run.is_current for run in runs] == [False, True]
        from sdb_identity.catalog_measurements import current_measurements_for_target

        current = current_measurements_for_target(session, runs[-1].target_id)[0]
        assert current.value == 7.2
        assert session.query(NormalizedMeasurement).count() == 1
        assert session.query(RawCatalogRow).count() == 2
        assert first.run_id != second.run_id


def test_query_coordinates_are_propagated_to_catalog_epoch(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=20)
    # Replace the target solution PM to isolate catalog query propagation.
    with session_factory.begin() as session:
        solution = session.get(__import__("sdb_identity.models", fromlist=["AstrometricSolution"]).AstrometricSolution, 1)
        solution.epoch = 2016.0
        solution.ra_deg = 10.0
        solution.dec_deg = 20.0
        solution.pm_ra_cosdec_masyr = 1000.0
        solution.pm_dec_masyr = -500.0
        solution.proper_motion_available = True
    adapter = FakeCatalog([])
    CatalogService(session_factory, {"2mass": adapter}).refresh(target.sdbid, "2mass")
    assert adapter.contexts[0].astrometry.epoch == 1999.3
    assert adapter.contexts[0].astrometry.ra_deg < 10.0


def test_bulk_refresh_chunks_queries_and_reuses_normal_scoring(session_factory):
    targets = [
        add_target(session_factory, ra_deg=10 + index, dec_deg=-20)
        for index in range(3)
    ]
    adapter = FakeBulkCatalog([])
    results = CatalogService(session_factory, {"2mass": adapter}).refresh_many(
        [target.target_id for target in targets], "2mass", chunk_size=2,
    )

    assert [len(batch) for batch in adapter.batches] == [2, 1]
    assert adapter.contexts == []
    assert [result.status for result in results] == ["match", "match", "match"]
    with session_factory() as session:
        requests = list(session.scalars(select(CatalogBatchRequest).order_by(
            CatalogBatchRequest.id
        )))
        runs = list(session.scalars(select(CatalogRun).order_by(CatalogRun.id)))
    assert [request.status for request in requests] == ["completed", "completed"]
    assert all(run.batch_request_id is not None for run in runs)


def test_failed_bulk_chunk_falls_back_to_individual_queries(session_factory):
    targets = [
        add_target(session_factory, ra_deg=10 + index, dec_deg=-20)
        for index in range(2)
    ]
    adapter = FakeBulkCatalog([])
    adapter.bulk_error = ProviderError("bulk timeout", transient=True)
    results = CatalogService(session_factory, {"2mass": adapter}).refresh_many(
        [target.target_id for target in targets], "2mass",
    )

    assert len(adapter.contexts) == 2
    assert [result.status for result in results] == ["no_match", "no_match"]
    with session_factory() as session:
        request = session.scalar(select(CatalogBatchRequest))
        runs = list(session.scalars(select(CatalogRun)))
    assert request.status == "fallback"
    assert request.error == "bulk timeout"
    assert {run.batch_request_id for run in runs} == {request.id}


def test_allwise_review_only_source_can_match_nearby_target_independently(session_factory):
    primary = add_target(session_factory, ra_deg=10.0, dec_deg=-20.0)
    # About 8 arcsec east at this declination: inside the AllWISE review cone,
    # outside the normal acceptance radius.
    companion_ra = 10.0 + 8.0 / (3600.0 * 0.9396926207859084)
    companion = add_target(session_factory, ra_deg=companion_ra, dec_deg=-20.0)

    adapter = AllWiseAdapter()

    def fake_query(context):
        candidate_row = adapter.parse_row({
            "AllWISE": "J004000.57-200000.0",
            "RAJ2000": companion_ra,
            "DEJ2000": -20.0,
            "W1mag": 8.0,
            "e_W1mag": 0.02,
            "qph": "AAAA",
            "ccf": "0000",
        })
        return [adapter.annotate_candidate(
            candidate_row,
            query_service="VizieR",
            query_catalog=adapter.release,
            query_radius_arcsec=adapter.query_radius(context),
            acceptance_radius_arcsec=adapter.acceptance_radius(context),
            context=context,
            expected=adapter.expected_source_ids(context.identifiers),
        )]

    adapter.query = fake_query
    service = CatalogService(session_factory, {"allwise": adapter})

    primary_result = service.refresh(primary.sdbid, "allwise")
    companion_result = service.refresh(companion.sdbid, "allwise")

    assert primary_result.status == "ambiguous"
    assert primary_result.measurement_count == 0
    assert companion_result.status == "match"
    assert companion_result.selected_source_id == "J004000.57-200000.0"
    assert companion_result.measurement_count == 1

    with session_factory() as session:
        rows = session.scalars(select(RawCatalogRow).order_by(RawCatalogRow.id)).all()
        measurements = session.scalars(select(NormalizedMeasurement)).all()

    assert len(rows) == 2
    assert {row.run_id for row in rows} == {primary_result.run_id, companion_result.run_id}
    associations = {
        row.run_id: json.loads(row.payload_json)["_sdb_association"]
        for row in rows
    }
    assert associations[primary_result.run_id]["review_only"] is True
    assert associations[companion_result.run_id]["review_only"] is False
    assert [measurement.target_id for measurement in measurements] == [companion.target_id]


def test_manual_catalog_candidate_override_is_append_only(session_factory):
    target = add_target(session_factory, ra_deg=10, dec_deg=-20)
    adapter = AllWiseAdapter()
    rows = [
        {
            "AllWISE": "J004000.00-200000.0", "RAJ2000": 10.00010,
            "DEJ2000": -20.0, "qph": "AAAA", "ccf": "0000",
            "W1mag": 7.1, "e_W1mag": 0.1,
        },
        {
            "AllWISE": "J004000.01-200000.0", "RAJ2000": 10.00011,
            "DEJ2000": -20.0, "qph": "AAAA", "ccf": "0000",
            "W1mag": 8.2, "e_W1mag": 0.2,
        },
    ]
    adapter.query = lambda context: [adapter.parse_row(row) for row in rows]
    service = CatalogService(session_factory, {"allwise": adapter})
    ambiguous = service.refresh(target.sdbid, "allwise")
    assert ambiguous.status == "ambiguous"
    with session_factory() as session:
        chosen = session.scalar(select(RawCatalogRow).where(
            RawCatalogRow.run_id == ambiguous.run_id,
            RawCatalogRow.source_id == "J004000.01-200000.0",
        ))
    replacement = service.override_candidate(
        chosen.id, actor="tester", reason="image inspection",
    )
    assert (replacement.status, replacement.measurement_count) == ("match", 1)
    with session_factory() as session:
        runs = list(session.scalars(select(CatalogRun).order_by(CatalogRun.id)))
        assert [run.is_current for run in runs] == [False, True]
        assert session.query(RawCatalogRow).count() == 4
        measurement = session.scalar(select(NormalizedMeasurement).where(
            NormalizedMeasurement.run_id == replacement.run_id
        ))
        assert measurement.value == 8.2
        audit = session.scalar(select(CatalogMatchOverride))
        assert audit.previous_run_id == ambiguous.run_id
        assert audit.replacement_run_id == replacement.run_id
        assert audit.reason == "image inspection"
        assert session.query(ExportDirtyTarget).where(
            ExportDirtyTarget.source_type == "catalog_override"
        ).count() == 1
        assert session.scalar(select(ExternalIdentifier).where(
            ExternalIdentifier.source == "allwise"
        )) is None


def test_catalog_reviewed_no_match_and_retry_are_audited(session_factory):
    target = IdentityService(session_factory).add(
        AddRequest(ra_deg=10, dec_deg=-20)
    )
    ambiguous_adapter = FakeCatalog([
        candidate("one", ra=10.00010),
        candidate("two", ra=10.00011),
    ])
    service = CatalogService(
        session_factory, {"2mass": ambiguous_adapter},
    )
    ambiguous = service.refresh(target.sdbid, "2mass")
    assert ambiguous.status == "ambiguous"

    reviewed = service.override_no_match(
        ambiguous.run_id,
        actor="reviewer",
        reason="neither candidate is the target",
    )
    assert reviewed.status == "no_match"
    with session_factory() as session:
        action = session.scalar(
            select(CatalogMatchOverride)
            .where(CatalogMatchOverride.action == "reviewed_no_match")
        )
        assert action.selected_source_id is None
        assert action.replacement_run_id == reviewed.run_id
        copied = list(session.scalars(
            select(RawCatalogRow).where(RawCatalogRow.run_id == reviewed.run_id)
        ))
        assert len(copied) == 2
        assert not any(row.accepted for row in copied)

    failing_adapter = FakeCatalog(
        [], error=ProviderError("temporary outage", transient=True),
    )
    retry_service = CatalogService(
        session_factory, {"2mass": failing_adapter},
    )
    failed = retry_service.refresh(target.sdbid, "2mass")
    assert failed.status == "transient_failure"
    failing_adapter.error = None
    failing_adapter.candidates = [
        candidate(measurements=[measurement()]),
    ]

    retried = retry_service.retry_failed_run(
        failed.run_id,
        actor="reviewer",
        reason="provider is available again",
    )
    assert retried.status == "match"
    with session_factory() as session:
        action = session.scalar(
            select(CatalogMatchOverride)
            .where(CatalogMatchOverride.action == "retry")
        )
        assert action.previous_run_id == failed.run_id
        assert action.replacement_run_id == retried.run_id
