from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select

from sdb_identity.hierarchy import HierarchyService
from sdb_identity.metadata import MetadataQueryResult, MetadataService, RelationshipValue
from sdb_identity.models import (
    Target,
    TargetLifecycleAction,
    TargetRelationship,
    TargetSystem,
    TargetSystemMember,
)
from sdb_identity.service import AddRequest, IdentityService
from sdb_identity.system_expansion import (
    import_immediate_relatives,
    preview_immediate_relatives,
)
from tests.fakes import FakeSimbad, astrometry, simbad_result
from tests.test_metadata import FakeMetadataProvider, snapshot


def _system_snapshot():
    return replace(
        snapshot("HD 1 AB"),
        identifiers=("HD 1", "WDS J00400-2000AB"),
        relationships=(
            RelationshipValue(
                "child", 201, "HD 1B", 10.001, -20.0,
                None, "2000A&A...000....1A", 3.38,
                related_object_type="Star",
                related_object_types=("Star", "PM*"),
            ),
            RelationshipValue(
                "parent", 301, "Moving Group 1", 10.1, -20.1,
                90, "2000A&A...000....2B", 490.0,
                related_object_type="MGr",
                related_object_types=("MGr",),
            ),
            RelationshipValue(
                "child", 401, "HD 1 b", 10.00001, -20.0,
                None, "2000A&A...000....3C", 0.04,
                related_object_type="Pl",
                related_object_types=("Pl",),
            ),
            RelationshipValue(
                "child", 501, "Unclassified 1", 10.002, -20.0,
                None, None, 6.8,
                related_object_type=None,
                related_object_types=(),
            ),
        ),
    )


def _component_snapshot():
    return replace(
        snapshot("HD 1 B"),
        oid=201,
        identifiers=("HD 1 B", "WDS J00400-2000B"),
        relationships=(
            RelationshipValue(
                "parent", 123, "HD 1 AB", 10.0005, -20.0,
                None, "2000A&A...000....1A", 1.69,
                related_object_type="**",
                related_object_types=("**",),
            ),
        ),
    )


def _root_with_metadata(session_factory):
    root = IdentityService(session_factory).add(AddRequest(ra_deg=10.0, dec_deg=-20.0))
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (_system_snapshot(),))),
    ).refresh(root.sdbid)
    return root


def test_relative_preview_separates_importable_context_and_unknown(session_factory):
    root = _root_with_metadata(session_factory)

    rows = preview_immediate_relatives(session_factory, root.sdbid)
    by_name = {row["main_id"]: row for row in rows}

    assert by_name["HD 1B"]["action"] == "import"
    assert by_name["HD 1B"]["component_label"] == "B"
    assert by_name["HD 1B"]["suggested_role"] == "physical"
    assert by_name["Moving Group 1"]["action"] == "context_only"
    assert by_name["Moving Group 1"]["component_relevance"] == "contextual_group"
    assert by_name["HD 1 b"]["action"] == "context_only"
    assert by_name["HD 1 b"]["component_relevance"] == "planetary_or_disk"
    assert by_name["Unclassified 1"]["action"] == "review_required"


def test_import_relatives_is_bounded_reconciles_system_and_is_idempotent(
    session_factory,
):
    root = _root_with_metadata(session_factory)
    identity = IdentityService(
        session_factory,
        simbad=FakeSimbad({
            "HD 1B": simbad_result(
                "HD 1B",
                astrometry(10.001, -20.0, source="simbad"),
                ("WDS J00400-2000B",),
            ),
        }),
    )

    first = import_immediate_relatives(
        session_factory,
        root.sdbid,
        identity_service=identity,
        actor="reviewer",
        reason="expand immediate stellar components",
    )
    second = import_immediate_relatives(
        session_factory,
        root.sdbid,
        identity_service=identity,
        actor="reviewer",
        reason="repeat safely",
    )

    assert (first.imported, first.context_only, first.review_required, first.failed) == (1, 2, 1, 0)
    assert (second.imported, second.already_imported, second.failed) == (0, 1, 0)
    with session_factory() as session:
        targets = list(session.scalars(select(Target).order_by(Target.id)))
        assert len(targets) == 2
        component = targets[1]
        system = session.scalar(select(TargetSystem))
        members = list(session.scalars(
            select(TargetSystemMember).order_by(TargetSystemMember.id)
        ))
        relationships = list(session.scalars(select(TargetRelationship)))
        lifecycle = list(session.scalars(
            select(TargetLifecycleAction).order_by(TargetLifecycleAction.id)
        ))

    assert system.primary_target_id == root.target_id
    assert {(row.target_id, row.component_label) for row in members} == {
        (root.target_id, "AB"),
        (component.id, "B"),
    }
    assert len(relationships) == 1
    assert relationships[0].parent_target_id == root.target_id
    assert relationships[0].child_target_id == component.id
    assert relationships[0].relationship_type == "simbad_parent_child"
    assert [(row.target_id, row.role, row.state) for row in lifecycle] == [
        (root.target_id, "composite", "system_only"),
        (component.id, "physical", "active"),
    ]
    context = HierarchyService(session_factory).system_context(root.sdbid)
    preview = {row["main_id"]: row for row in context["simbad_relative_preview"]}
    assert preview["HD 1B"]["action"] == "already_imported"
    assert preview["HD 1B"]["matched_sdbid"] == component.sdbid


def test_importing_component_first_promotes_composite_parent_and_system_name(
    session_factory,
):
    component = IdentityService(session_factory).add(
        AddRequest(ra_deg=10.001, dec_deg=-20.0)
    )
    MetadataService(
        session_factory,
        FakeMetadataProvider(MetadataQueryResult("match", (_component_snapshot(),))),
    ).refresh(component.sdbid)
    identity = IdentityService(
        session_factory,
        simbad=FakeSimbad({
            "HD 1 AB": simbad_result(
                "HD 1 AB",
                astrometry(10.0005, -20.0, source="simbad"),
                ("WDS J00400-2000AB",),
            ),
        }),
    )

    result = import_immediate_relatives(
        session_factory,
        component.sdbid,
        identity_service=identity,
        actor="reviewer",
        reason="expand from component first",
    )

    assert result.imported == 1
    assert result.system_name == "HD 1 AB system"
    with session_factory() as session:
        targets = list(session.scalars(select(Target).order_by(Target.id)))
        parent = targets[1]
        system = session.scalar(select(TargetSystem))
        members = list(session.scalars(
            select(TargetSystemMember).order_by(TargetSystemMember.id)
        ))
        lifecycle = list(session.scalars(
            select(TargetLifecycleAction).order_by(TargetLifecycleAction.id)
        ))

    assert system.name == "HD 1 AB system"
    assert system.primary_target_id == parent.id
    assert {(row.target_id, row.component_label) for row in members} == {
        (component.target_id, "B"),
        (parent.id, "AB"),
    }
    assert [(row.target_id, row.role, row.state) for row in lifecycle] == [
        (component.target_id, "physical", "active"),
        (parent.id, "composite", "system_only"),
    ]
