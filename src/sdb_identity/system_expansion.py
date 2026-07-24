from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import angular_separation_arcsec
from .dirty import find_target, mark_export_dirty
from .hierarchy import (
    HierarchyService,
    _component_label_from_identifier,
    _simbad_component_relevance,
)
from .models import (
    ExternalIdentifier,
    MetadataRun,
    SimbadMetadata,
    SimbadRelationship,
    StructuralEdge,
    Target,
    TargetSystem,
    TargetSystemMember,
)
from .providers import Astrometry
from .service import AddRequest, IdentityService, UnresolvedTarget, normalize_identifier
from .target_lifecycle import set_target_lifecycle, target_lifecycle_status


@dataclass(frozen=True)
class RelativeImportResult:
    requested_sdbid: str
    system_id: int | None
    system_name: str | None
    imported: int
    already_imported: int
    context_only: int
    review_required: int
    failed: int
    relatives: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["relatives"] = list(self.relatives)
        return value


def preview_immediate_relatives(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
) -> list[dict[str, object]]:
    """Classify current, immediate SIMBAD relationships without recursion."""
    with session_factory() as session:
        target = find_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        run = session.scalar(
            select(MetadataRun)
            .where(
                MetadataRun.target_id == target.id,
                MetadataRun.provider == "simbad",
                MetadataRun.is_current.is_(True),
            )
            .order_by(MetadataRun.id.desc())
            .limit(1)
        )
        if run is None:
            raise ValueError("target has no current SIMBAD metadata; run sdb update --providers simbad")
        if run.status != "match":
            raise ValueError(f"current SIMBAD metadata status is {run.status}, not match")
        relationships = list(session.scalars(
            select(SimbadRelationship)
            .where(SimbadRelationship.run_id == run.id)
            .order_by(
                SimbadRelationship.direction,
                SimbadRelationship.separation_arcsec,
                SimbadRelationship.related_main_id,
                SimbadRelationship.id,
            )
        ))
        result = []
        for relationship in relationships:
            object_types = json.loads(relationship.related_object_types_json or "[]")
            relevance = _simbad_component_relevance(
                relationship.related_object_type, object_types,
            )
            matched = _find_related_target(session, relationship)
            component = _component_label_from_identifier(relationship.related_main_id)
            role = _suggested_role(relationship.direction, component)
            if relevance in {"contextual_group", "planetary_or_disk"}:
                action = "context_only"
                reason = f"{relevance} relationships do not create SDB stellar targets"
            elif relevance == "stellar_or_substellar_component" and matched is not None:
                action = "already_imported"
                reason = "SIMBAD OID/main identity or component-consistent position matches an SDB target"
            elif relevance == "stellar_or_substellar_component":
                action = "import"
                reason = "immediate SIMBAD relative is stellar or substellar"
            else:
                action = "review_required"
                reason = "SIMBAD object types do not establish a stellar structural component"
            result.append({
                "relationship_id": relationship.id,
                "direction": relationship.direction,
                "related_oid": relationship.related_oid,
                "main_id": relationship.related_main_id,
                "ra_deg": relationship.related_ra_deg,
                "dec_deg": relationship.related_dec_deg,
                "separation_arcsec": relationship.separation_arcsec,
                "object_type": relationship.related_object_type,
                "object_types": object_types,
                "component_relevance": relevance,
                "component_label": component,
                "suggested_role": role,
                "suggested_state": "system_only" if role == "composite" else "active",
                "action": action,
                "reason": reason,
                "matched_target_id": None if matched is None else matched.id,
                "matched_sdbid": None if matched is None else matched.sdbid,
                "bibcode": relationship.link_bibcode,
            })
        return result


def import_immediate_relatives(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    identity_service: IdentityService,
    actor: str,
    reason: str,
) -> RelativeImportResult:
    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required")
    preview = preview_immediate_relatives(session_factory, target_reference)
    with session_factory() as session:
        requested = find_target(session, target_reference)
        if requested is None:
            raise KeyError(f"target not found: {target_reference}")
        requested_sdbid = requested.sdbid

    hierarchy = HierarchyService(session_factory)
    system = _find_or_create_expansion_system(
        session_factory, hierarchy, requested_sdbid,
    )
    _apply_inferred_lifecycle(
        session_factory,
        hierarchy,
        requested_sdbid,
        system_id=system.id,
        actor=actor,
        reason=reason,
    )
    rows = []
    counts = {
        "imported": 0,
        "already_imported": 0,
        "context_only": 0,
        "review_required": 0,
        "failed": 0,
    }
    for relative in preview:
        row = dict(relative)
        if relative["action"] in {"context_only", "review_required"}:
            counts[str(relative["action"])] += 1
            rows.append(row)
            continue
        target_sdbid = relative.get("matched_sdbid")
        if target_sdbid is None:
            try:
                added = identity_service.add(AddRequest(
                    name=str(relative["main_id"]),
                    command=f"hierarchy import-relatives {requested_sdbid}",
                ))
            except (UnresolvedTarget, ValueError) as error:
                row["action"] = "failed"
                row["error"] = str(error)
                counts["failed"] += 1
                rows.append(row)
                continue
            target_sdbid = added.sdbid
            row["matched_target_id"] = added.target_id
            row["matched_sdbid"] = added.sdbid
            row["action"] = "imported" if added.created else "already_imported"
        counts[str(row["action"])] += 1
        hierarchy.add_member(
            system.name,
            target_sdbid,
            component_label=relative.get("component_label"),
            source="simbad",
        )
        _set_lifecycle_if_changed(
            session_factory,
            target_sdbid,
            role=str(relative["suggested_role"]),
            state=str(relative["suggested_state"]),
            actor=actor,
            reason=reason,
        )
        _ensure_simbad_relationship(
            session_factory,
            system,
            requested_sdbid=requested_sdbid,
            relative_sdbid=target_sdbid,
            relative=relative,
            actor=actor,
            reason=reason,
        )
        if relative["direction"] == "parent" and relative["suggested_role"] == "composite":
            system.name = _promote_system_primary(
                session_factory, system.id, target_sdbid,
            )
        rows.append(row)

    return RelativeImportResult(
        requested_sdbid=requested_sdbid,
        system_id=system.id,
        system_name=system.name,
        imported=counts["imported"],
        already_imported=counts["already_imported"],
        context_only=counts["context_only"],
        review_required=counts["review_required"],
        failed=counts["failed"],
        relatives=tuple(rows),
    )


def _find_related_target(
    session: Session,
    relationship: SimbadRelationship,
) -> Target | None:
    metadata = session.scalar(
        select(SimbadMetadata)
        .join(MetadataRun, MetadataRun.id == SimbadMetadata.run_id)
        .where(
            SimbadMetadata.oid == relationship.related_oid,
            MetadataRun.is_current.is_(True),
            MetadataRun.status == "match",
        )
        .order_by(MetadataRun.id.desc())
        .limit(1)
    )
    if metadata is not None:
        return session.get(Target, metadata.target_id)
    normalized = normalize_identifier(relationship.related_main_id)
    candidate_ids = set(session.scalars(select(ExternalIdentifier.target_id).where(
        ExternalIdentifier.normalized_value == normalized
    )))
    for target_id in sorted(candidate_ids):
        target = session.get(Target, target_id)
        if target is None:
            continue
        primary = IdentityService._target_primary_identity(session, target)
        if primary and normalize_identifier(primary) == normalized:
            return target
    if relationship.related_ra_deg is None or relationship.related_dec_deg is None:
        return None
    related_component = _component_label_from_identifier(relationship.related_main_id)
    position = Astrometry(relationship.related_ra_deg, relationship.related_dec_deg)
    radius_deg = 1.0 / 3600.0
    candidates = session.scalars(select(Target).where(
        Target.dec2000_deg.between(
            relationship.related_dec_deg - radius_deg,
            relationship.related_dec_deg + radius_deg,
        )
    ))
    for target in candidates:
        primary = IdentityService._target_primary_identity(session, target)
        primary_component = _component_label_from_identifier(primary or "")
        if related_component != primary_component and (
            related_component is not None or primary_component is not None
        ):
            continue
        if angular_separation_arcsec(
            position, Astrometry(target.ra2000_deg, target.dec2000_deg)
        ) <= 1.0:
            return target
    return None


def _suggested_role(direction: str, component: str | None) -> str:
    if direction == "parent" or _group_component(component):
        return "composite"
    return "physical"


def _group_component(component: str | None) -> bool:
    if not component:
        return False
    return "," in component or (
        len(component) > 1 and component.isalpha() and component.isupper()
    )


def _find_or_create_expansion_system(
    session_factory: sessionmaker[Session],
    hierarchy: HierarchyService,
    requested_sdbid: str,
) -> TargetSystem:
    with session_factory() as session:
        requested = find_target(session, requested_sdbid)
        row = session.scalar(
            select(TargetSystem)
            .join(TargetSystemMember, TargetSystemMember.system_id == TargetSystem.id)
            .where(TargetSystemMember.target_id == requested.id)
            .order_by(TargetSystem.id)
            .limit(1)
        )
        if row is not None:
            return row
        primary_identity = IdentityService._target_primary_identity(session, requested)
        base_name = f"{primary_identity or requested.sdbid} system"
        name = base_name
        index = 2
        while session.scalar(select(TargetSystem.id).where(TargetSystem.name == name)) is not None:
            name = f"{base_name} {index}"
            index += 1
    return hierarchy.create_system(
        name,
        primary=requested_sdbid,
        source="simbad",
        note="created by bounded immediate SIMBAD-relative expansion",
    )


def _apply_inferred_lifecycle(
    session_factory: sessionmaker[Session],
    hierarchy: HierarchyService,
    target_sdbid: str,
    *,
    system_id: int,
    actor: str,
    reason: str,
) -> None:
    context = hierarchy.target_context(target_sdbid)
    kind = str(context["semantic_identity"].get("kind") or "unknown")
    component = context["component_assignment"].get("semantic_component")
    if component:
        with session_factory.begin() as session:
            target = find_target(session, target_sdbid)
            member = session.scalar(select(TargetSystemMember).where(
                TargetSystemMember.system_id == system_id,
                TargetSystemMember.target_id == target.id,
            ).order_by(TargetSystemMember.id).limit(1))
            if member is not None and member.component_label is None:
                member.component_label = str(component)
    if kind in {"system_or_parent", "subsystem"}:
        _set_lifecycle_if_changed(
            session_factory, target_sdbid,
            role="composite", state="system_only", actor=actor, reason=reason,
        )
    elif kind == "component":
        _set_lifecycle_if_changed(
            session_factory, target_sdbid,
            role="physical", state="active", actor=actor, reason=reason,
        )


def _set_lifecycle_if_changed(
    session_factory: sessionmaker[Session],
    target_sdbid: str,
    *,
    role: str,
    state: str,
    actor: str,
    reason: str,
) -> None:
    current = target_lifecycle_status(session_factory, target_sdbid)
    if current.role == role and current.state == state:
        return
    set_target_lifecycle(
        session_factory,
        target_sdbid,
        role=role,
        state=state,
        actor=actor,
        reason=reason,
    )


def _ensure_simbad_relationship(
    session_factory: sessionmaker[Session],
    system: TargetSystem,
    *,
    requested_sdbid: str,
    relative_sdbid: str,
    relative: dict[str, object],
    actor: str,
    reason: str,
) -> None:
    with session_factory.begin() as session:
        requested = find_target(session, requested_sdbid)
        related = find_target(session, relative_sdbid)
        if relative["direction"] == "child":
            parent_id, child_id = requested.id, related.id
        else:
            parent_id, child_id = related.id, requested.id
        existing = session.scalar(select(StructuralEdge.id).where(
            StructuralEdge.system_id == system.id,
            StructuralEdge.endpoint_a_target_id == parent_id,
            StructuralEdge.endpoint_b_target_id == child_id,
            StructuralEdge.direction == "a_parent_b",
            StructuralEdge.relation_type == "simbad_parent_child",
            StructuralEdge.status == "accepted",
        ).limit(1))
        if existing is not None:
            return
        relationship = StructuralEdge(
            source="simbad",
            system_id=system.id,
            endpoint_a_target_id=parent_id,
            endpoint_b_target_id=child_id,
            direction="a_parent_b",
            relation_type="simbad_parent_child",
            component_label=relative.get("component_label"),
            separation_arcsec=relative.get("separation_arcsec"),
            confidence="provider",
            status="accepted",
            actor=actor.strip(),
            reason=reason.strip(),
        )
        session.add(relationship)
        session.flush()
        for target_id in {parent_id, child_id}:
            mark_export_dirty(
                session,
                target_id,
                source_type="hierarchy_relationship",
                source_id=relationship.id,
                reason="SIMBAD target relationship changed",
            )


def _promote_system_primary(
    session_factory: sessionmaker[Session],
    system_id: int,
    target_sdbid: str,
) -> str:
    with session_factory.begin() as session:
        system = session.get(TargetSystem, system_id)
        target = find_target(session, target_sdbid)
        system.primary_target_id = target.id
        primary_identity = IdentityService._target_primary_identity(session, target)
        preferred_name = None if not primary_identity else f"{primary_identity} system"
        if preferred_name and preferred_name != system.name:
            conflict = session.scalar(select(TargetSystem.id).where(
                TargetSystem.name == preferred_name,
                TargetSystem.id != system.id,
            ))
            if conflict is None:
                system.name = preferred_name
        return system.name
