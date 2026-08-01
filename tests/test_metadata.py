from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from sdb_identity.metadata import (
    MetadataQueryContext,
    MetadataQueryResult,
    MetadataService,
    ObjectTypeValue,
    RelationshipValue,
    SimbadSnapshot,
)
from sdb_identity.models.identity import ExternalIdentifier
from sdb_identity.models.metadata import (
    MetadataRun,
    SimbadMetadata,
    SimbadObjectType,
    SimbadRelationship,
    UserNote,
)
from sdb_identity.providers import ProviderError
from sdb_identity.service import AddRequest, IdentityService


@dataclass
class FakeMetadataProvider:
    result: MetadataQueryResult | None = None
    error: ProviderError | None = None
    name: str = "simbad"
    release: str = "fake-simbad"

    def __post_init__(self):
        self.contexts: list[MetadataQueryContext] = []

    def query(self, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.result or MetadataQueryResult("no_match")


class FakeBulkMetadataProvider(FakeMetadataProvider):
    def query_many(self, contexts):
        self.contexts.extend(contexts)
        if self.error:
            raise self.error
        return {
            context.target_id: self.result or MetadataQueryResult("no_match")
            for context in contexts
        }


def snapshot(main_id="HD 1", spectral_type="F5V"):
    return SimbadSnapshot(
        oid=123,
        main_id=main_id,
        ra_deg=10.0,
        dec_deg=-20.0,
        identifiers=("HD 1", "2MASS J00000000-2000000", "TYC 1-2-3"),
        spectral_type=spectral_type,
        spectral_type_bibcode="2000A&A...000....1A",
        parallax_mas=12.3,
        parallax_error_mas=0.2,
        parallax_bibcode="2020A&A...000....2B",
        pm_ra_cosdec_masyr=123.4,
        pm_dec_masyr=-56.7,
        proper_motion_bibcode="2021A&A...000....6P",
        radial_velocity_kms=22.0,
        radial_velocity_error_kms=1.0,
        radial_velocity_bibcode="2010A&A...000....3C",
        primary_object_type="Star",
        object_types=(
            ObjectTypeValue("Star", "*", "Star", True),
            ObjectTypeValue("PM*", "PM*", "High proper-motion Star", False),
        ),
        relationships=(
            RelationshipValue(
                "parent", 456, "Cluster 1", 10.1, -20.1,
                80, "2015A&A...000....4D", 493.9,
                related_object_type="Cl*",
                related_object_types=("Cl*",),
            ),
            RelationshipValue(
                "child", 789, "HD 1 B", 10.0001, -20.0,
                None, None, 0.34,
                related_object_type="Star",
                related_object_types=("Star", "PM*"),
                related_spectral_type="K0V",
                related_spectral_type_bibcode="2001A&A...000....5E",
            ),
        ),
        raw={"basic": {"oid": 123, "main_id": main_id}, "alltypes": "Star|PM*"},
    )


def add_target(session_factory):
    return IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))


def test_metadata_refresh_stores_versioned_simbad_data(session_factory):
    target = add_target(session_factory)
    provider = FakeMetadataProvider(MetadataQueryResult("match", (snapshot(),)))
    result = MetadataService(session_factory, provider).refresh(target.sdbid)
    assert result.status == "match"
    assert result.main_id == "HD 1"
    with session_factory() as session:
        metadata = session.scalars(select(SimbadMetadata)).one()
        assert metadata.spectral_type == "F5V"
        assert metadata.parallax_mas == 12.3
        assert metadata.pm_ra_cosdec_masyr == 123.4
        assert metadata.pm_dec_masyr == -56.7
        assert metadata.proper_motion_bibcode == "2021A&A...000....6P"
        assert metadata.radial_velocity_kms == 22.0
        assert session.query(SimbadObjectType).count() == 2
        relationships = session.scalars(select(SimbadRelationship).order_by(SimbadRelationship.direction)).all()
        assert len(relationships) == 2
        child = next(value for value in relationships if value.direction == "child")
        assert child.related_object_type == "Star"
        assert child.related_object_types_json == '["Star", "PM*"]'
        assert child.related_spectral_type == "K0V"
        assert child.related_spectral_type_bibcode == "2001A&A...000....5E"
        identifiers = set(
            session.scalars(
                select(ExternalIdentifier.value).where(
                    ExternalIdentifier.source == "simbad_metadata"
                )
            )
        )
        assert {"HD 1", "2MASS J00000000-2000000", "TYC 1-2-3"} <= identifiers


def test_metadata_refresh_many_uses_bulk_provider_and_stores_results(session_factory):
    first = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=-20))
    second = IdentityService(session_factory).add(AddRequest(ra_deg=20, dec_deg=-20))
    provider = FakeBulkMetadataProvider(MetadataQueryResult("match", (snapshot(),)))

    results = MetadataService(session_factory, provider).refresh_many(
        [first.target_id, second.target_id]
    )

    assert [result.status for result in results] == ["match", "match"]
    assert [context.target_id for context in provider.contexts] == [
        first.target_id,
        second.target_id,
    ]
    with session_factory() as session:
        assert session.query(SimbadMetadata).count() == 2
        assert session.query(MetadataRun).filter(MetadataRun.is_current.is_(True)).count() == 2


def test_no_match_and_ambiguous_are_recorded_without_metadata(session_factory):
    target = add_target(session_factory)
    service = MetadataService(session_factory, FakeMetadataProvider(MetadataQueryResult("no_match")))
    assert service.refresh(target.sdbid).status == "no_match"
    service.provider = FakeMetadataProvider(MetadataQueryResult("ambiguous", (snapshot(), snapshot("HD 2"))))
    assert service.refresh(target.sdbid).status == "ambiguous"
    with session_factory() as session:
        assert session.query(SimbadMetadata).count() == 0
        runs = session.scalars(select(MetadataRun).order_by(MetadataRun.id)).all()
        assert [run.is_current for run in runs] == [False, True]


def test_transient_failure_preserves_previous_current_metadata(session_factory):
    target = add_target(session_factory)
    service = MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (snapshot(),))),
    )
    first = service.refresh(target.sdbid)
    service.provider = FakeMetadataProvider(error=ProviderError("timeout", transient=True))
    failed = service.refresh(target.sdbid)
    assert failed.status == "transient_failure"
    with session_factory() as session:
        current = session.scalars(select(MetadataRun).where(MetadataRun.is_current.is_(True))).all()
        assert [run.id for run in current] == [first.run_id]


def test_refresh_versions_metadata_and_preserves_user_notes(session_factory):
    target = add_target(session_factory)
    service = MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (snapshot(),))),
    )
    first = service.refresh(target.sdbid)
    note = service.add_note(target.sdbid, "Check unresolved companion", actor="grant")
    service.provider = FakeMetadataProvider(MetadataQueryResult("match", (snapshot(spectral_type="F6V"),)))
    second = service.refresh(target.sdbid)
    assert first.run_id != second.run_id
    with session_factory() as session:
        runs = session.scalars(select(MetadataRun).order_by(MetadataRun.id)).all()
        assert [run.is_current for run in runs] == [False, True]
        assert session.get(UserNote, note.id).text == "Check unresolved companion"
        current = session.scalar(
            select(SimbadMetadata).where(SimbadMetadata.run_id == second.run_id)
        )
        assert current.spectral_type == "F6V"


def test_note_listing_is_append_only(session_factory):
    target = add_target(session_factory)
    service = MetadataService(session_factory, FakeMetadataProvider())
    first = service.add_note(target.sdbid, "First", actor="a")
    second = service.add_note(target.sdbid, "Second", actor="b")
    notes = service.list_notes(target.sdbid)
    assert [note.id for note in notes] == [first.id, second.id]
    assert [note.text for note in notes] == ["First", "Second"]
