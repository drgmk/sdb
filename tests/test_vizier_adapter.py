from __future__ import annotations

import time

import pytest
from astropy.table import Table

from sdb_identity.catalogs.adapters.vizier import VizierConeAdapter
from sdb_identity.catalogs.types import CatalogCandidate, CatalogQueryContext
from sdb_identity.providers import Astrometry, ProviderError


class FakeVizierAdapter(VizierConeAdapter):
    name = "fake"
    display_name = "Fake Catalog"
    release = "fake/release"
    query_epoch = 2000.0
    source_id_columns = ("id",)
    query_many_workers = 2

    def __init__(self, *, error: Exception | None = None, delay: float = 0.0):
        self.error = error
        self.delay = delay
        self.seen = []

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
        self.seen.append(context.target_id)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return [
            CatalogCandidate(
                source_id=f"source-{context.target_id}",
                ra_deg=context.astrometry.ra_deg,
                dec_deg=context.astrometry.dec_deg,
                epoch=self.query_epoch,
                payload={},
            )
        ]

    def parse_row(self, row):
        raise NotImplementedError


class FakeMultiConeAdapter(VizierConeAdapter):
    name = "fake_multi"
    display_name = "Fake Multi Catalog"
    release = "fake/multi"
    query_epoch = 2000.0
    source_id_columns = ("id",)
    multicone_chunk_size = 2

    def __init__(self):
        self.calls = []

    def create_client(self):
        owner = self

        class Client:
            TIMEOUT = None

            def query_region(self, coordinates, *, radius, catalog):
                owner.calls.append((len(coordinates), radius.to_value("arcsec"), catalog))
                return [Table({
                    "_q": list(range(1, len(coordinates) + 1)),
                    "id": [f"row-{len(owner.calls)}-{index}" for index in range(len(coordinates))],
                    "ra": coordinates.ra.deg,
                    "dec": coordinates.dec.deg,
                })]

        return Client()

    def parse_row(self, row):
        return CatalogCandidate(
            source_id=str(row["id"]),
            ra_deg=float(row["ra"]),
            dec_deg=float(row["dec"]),
            epoch=self.query_epoch,
            payload={},
        )


def context(target_id: int) -> CatalogQueryContext:
    return CatalogQueryContext(
        target_id=target_id,
        sdbid=f"target-{target_id}",
        astrometry=Astrometry(10.0 + target_id, -20.0, epoch=2000.0),
    )


def test_vizier_query_many_returns_candidates_by_target():
    adapter = FakeVizierAdapter()
    result = adapter.query_many((context(1), context(2), context(3)))

    assert set(result) == {1, 2, 3}
    assert result[1][0].source_id == "source-1"
    assert result[2][0].source_id == "source-2"
    assert sorted(adapter.seen) == [1, 2, 3]


def test_vizier_query_many_is_bounded_and_concurrent():
    adapter = FakeVizierAdapter(delay=0.05)
    started = time.perf_counter()
    adapter.query_many((context(1), context(2), context(3), context(4)))
    elapsed = time.perf_counter() - started

    # Four serial 50 ms calls would be about 0.2 s. With the adapter's two-worker
    # bound this should complete in roughly two waves, while keeping the assertion
    # loose enough for loaded CI machines.
    assert elapsed < 0.18


def test_vizier_query_many_propagates_provider_error():
    adapter = FakeVizierAdapter(error=ProviderError("timeout", transient=True))

    with pytest.raises(ProviderError, match="timeout") as caught:
        adapter.query_many((context(1),))

    assert caught.value.transient is True


def test_vizier_query_many_wraps_unexpected_error():
    adapter = FakeVizierAdapter(error=RuntimeError("boom"))

    with pytest.raises(ProviderError, match="target-1: boom") as caught:
        adapter.query_many((context(1),))

    assert caught.value.transient is True


def test_vizier_multicone_batches_and_distributes_rows_by_query_index():
    adapter = FakeMultiConeAdapter()
    result = adapter.query_many_vizier(tuple(context(index) for index in range(1, 6)))

    assert [call[0] for call in adapter.calls] == [2, 2, 1]
    assert set(result) == {1, 2, 3, 4, 5}
    assert all(len(rows) == 1 for rows in result.values())
    assert result[1][0].payload["_sdb_association"]["query_service"] == "VizieR multi-cone"


def test_vizier_annotation_marks_review_only_and_scoring_ignores_it():
    adapter = FakeVizierAdapter()
    adapter.review_radius_arcsec = 10.0
    query_context = context(1)
    candidate = CatalogCandidate(
        source_id="source-1",
        ra_deg=query_context.astrometry.ra_deg + 5.0 / 3600.0,
        dec_deg=query_context.astrometry.dec_deg,
        epoch=adapter.query_epoch,
        payload={},
    )

    annotated = adapter.annotate_candidate(
        candidate,
        context=query_context,
        query_radius_arcsec=adapter.query_radius(query_context),
        acceptance_radius_arcsec=adapter.acceptance_radius(query_context),
        expected=set(),
    )

    association = annotated.payload["_sdb_association"]
    provenance = annotated.provenance[0]
    assert adapter.query_radius(query_context) == 10.0
    assert association["acceptance_radius_arcsec"] == adapter.radius_arcsec
    assert association["candidate_separation_arcsec"] > adapter.radius_arcsec
    assert association["review_only"] is True
    assert provenance.identifier_column == "id"
    assert provenance.identifier_value == "source-1"
    assert provenance.access_url.endswith(
        "fake%2Frelease&id===source-1"
    )
    assert adapter.score_candidate(
        query_context, annotated, association["candidate_separation_arcsec"]
    ) == 0.0
