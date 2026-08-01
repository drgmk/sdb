from __future__ import annotations

from sdb_identity.hierarchy.service import HierarchyService
from sdb_identity.providers import SimbadNeighbour
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.target_import import (
    TargetImportService,
    search_nearby_simbad,
)
from sdb_identity.update import UpdateItem, UpdateSummary
from tests.fakes import FakeGaia, FakeSimbad, astrometry, simbad_result


class FakeUpdateService:
    def __init__(self):
        self.calls = []

    def update_targets(self, targets, *, providers, force):
        self.calls.append((tuple(targets), tuple(providers), force))
        items = tuple(
            UpdateItem(
                None,
                target,
                provider,
                "refreshed",
                "match",
            )
            for target in targets
            for provider in providers
        )
        return UpdateSummary(
            target_count=len(targets),
            refreshed=len(items),
            skipped=0,
            missing=0,
            failed=0,
            items=items,
        )


def test_nearby_simbad_search_marks_current_and_new_candidates(
    session_factory,
):
    root = IdentityService(
        session_factory,
        simbad=FakeSimbad({
            "HD 1": simbad_result(
                "HD   1",
                astrometry(10.0, -20.0, source="simbad"),
            ),
        }),
        gaia=FakeGaia(),
    ).add(AddRequest(name="HD 1"))
    provider = FakeSimbad(neighbours=[
        SimbadNeighbour(
            1,
            "HD   1",
            astrometry(10.0, -20.0, source="simbad"),
            0.0,
            primary_object_type="Star",
            object_type_label="Star",
        ),
        SimbadNeighbour(
            2,
            "HD   1B",
            astrometry(10.001, -20.0, source="simbad"),
            3.38,
            primary_object_type="Star",
            object_type_label="Star",
            spectral_type="M3V",
        ),
        SimbadNeighbour(
            3,
            "HD   1b",
            astrometry(10.0, -20.0, source="simbad"),
            0.0,
            primary_object_type="Planet",
            object_type_label="Planet",
            object_types=("Planet",),
        ),
    ])

    result = search_nearby_simbad(
        session_factory,
        root.sdbid,
        provider=provider,
        radius_arcsec=60,
    )

    assert result.target_sdbid == root.sdbid
    assert [row.main_id for row in result.candidates] == [
        "HD   1", "HD   1b", "HD   1B",
    ]
    assert result.candidates[0].current_target is True
    assert result.candidates[0].selectable is False
    assert result.candidates[1].blocked_reason == "planet"
    assert result.candidates[1].selectable is False
    assert result.candidates[2].existing_sdbid is None
    assert result.candidates[2].selectable is True
    assert provider.region_calls[0][1:] == (60, 100)


def test_ensured_target_import_is_bulk_updated_partial_and_idempotent(
    session_factory,
):
    simbad = FakeSimbad({
        "HD 1B": simbad_result(
            "HD   1B",
            astrometry(10.001, -20.0, source="simbad"),
        ),
    })
    update = FakeUpdateService()
    service = TargetImportService(
        session_factory,
        identity_service=IdentityService(
            session_factory,
            simbad=simbad,
            gaia=FakeGaia(),
        ),
        update_service=update,
        hierarchy_service=HierarchyService(session_factory),
    )

    first = service.import_many(
        ["HD 1B", "Unknown"],
        providers=("simbad", "gaia_dr3", "2mass"),
        command="review import near root",
    )

    assert first.created_count == 1
    assert first.failed_count == 1
    assert first.items[1].error == "name could not be resolved to coordinates"
    assert update.calls == [(
        (first.items[0].sdbid,),
        ("simbad", "gaia_dr3", "2mass"),
        False,
    )]
    assert [row["provider"] for row in first.hierarchy_matches] == [
        "wds", "ccdm",
    ]

    second = service.import_many(
        ["HD 1B"],
        providers=("simbad", "gaia_dr3", "2mass"),
    )

    assert second.created_count == 0
    assert second.existing_count == 1
    assert second.items[0].sdbid == first.items[0].sdbid
