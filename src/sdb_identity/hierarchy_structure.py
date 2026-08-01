from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .dirty import mark_export_dirty
from .models import StructuralEdge, Target, TargetSystem, TargetSystemMember
from .targets import resolve_target


RELATIONSHIP_STATUS = "accepted"


@dataclass(frozen=True)
class SystemMember:
    target_id: int
    sdbid: str
    component_label: str | None
    source: str


@dataclass(frozen=True)
class RelationshipSummary:
    id: int
    relationship_type: str
    source: str
    status: str
    confidence: str
    component: str | None
    parent_sdbid: str | None
    child_sdbid: str | None
    primary_sdbid: str | None
    secondary_sdbid: str | None
    separation_arcsec: float | None
    pa_deg: float | None
    relation_epoch: float | None
    reason: str
    actor: str | None


@dataclass(frozen=True)
class HierarchyStatus:
    target_id: int
    sdbid: str
    systems: tuple[dict[str, object], ...]
    relationships: tuple[RelationshipSummary, ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["relationships"] = [asdict(item) for item in self.relationships]
        return value


class HierarchyStructureService:
    """Manage explicit target systems and accepted target relationships."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def create_system(
        self,
        name: str,
        *,
        primary: str | int | None = None,
        source: str = "manual",
        note: str | None = None,
    ) -> TargetSystem:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("system name is required")
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(TargetSystem).where(TargetSystem.name == clean_name)
            )
            if existing is not None:
                raise ValueError(f"system already exists: {clean_name}")
            primary_target = (
                _find_required_target(session, primary)
                if primary is not None
                else None
            )
            system = TargetSystem(
                name=clean_name,
                primary_target_id=None
                if primary_target is None
                else primary_target.id,
                source=source,
                note=note,
            )
            session.add(system)
            session.flush()
            if primary_target is not None:
                session.add(
                    TargetSystemMember(
                        system_id=system.id,
                        target_id=primary_target.id,
                        component_label=None,
                        source=source,
                    )
                )
                mark_export_dirty(
                    session,
                    primary_target.id,
                    source_type="hierarchy_system",
                    source_id=system.id,
                    reason="target system membership changed",
                )
            return system

    def add_member(
        self,
        system_name: str,
        target_reference: str | int,
        *,
        component_label: str | None = None,
        source: str = "manual",
    ) -> TargetSystemMember:
        with self.session_factory.begin() as session:
            system = _find_required_system(session, system_name)
            target = _find_required_target(session, target_reference)
            existing = session.scalar(
                select(TargetSystemMember).where(
                    TargetSystemMember.system_id == system.id,
                    TargetSystemMember.target_id == target.id,
                    TargetSystemMember.component_label == component_label,
                )
            )
            if existing is not None:
                return existing
            member = TargetSystemMember(
                system_id=system.id,
                target_id=target.id,
                component_label=component_label,
                source=source,
            )
            session.add(member)
            mark_export_dirty(
                session,
                target.id,
                source_type="hierarchy_membership",
                source_id=system.id,
                reason="target system membership changed",
            )
            session.flush()
            return member

    def add_relationship(
        self,
        *,
        relationship_type: str,
        primary: str | int | None = None,
        secondary: str | int | None = None,
        parent: str | int | None = None,
        child: str | int | None = None,
        system: str | None = None,
        component: str | None = None,
        source: str = "manual",
        separation_arcsec: float | None = None,
        pa_deg: float | None = None,
        relation_epoch: float | None = None,
        confidence: str = "manual",
        status: str = "current",
        actor: str | None = None,
        reason: str = "",
    ) -> StructuralEdge:
        if not relationship_type.strip():
            raise ValueError("relationship type is required")
        with self.session_factory.begin() as session:
            system_row = (
                _find_required_system(session, system)
                if system is not None
                else None
            )
            primary_target = (
                _find_required_target(session, primary)
                if primary is not None
                else None
            )
            secondary_target = (
                _find_required_target(session, secondary)
                if secondary is not None
                else None
            )
            parent_target = (
                _find_required_target(session, parent)
                if parent is not None
                else None
            )
            child_target = (
                _find_required_target(session, child)
                if child is not None
                else None
            )
            if all(
                target is None
                for target in (
                    primary_target,
                    secondary_target,
                    parent_target,
                    child_target,
                )
            ):
                raise ValueError("relationship must reference at least one target")
            if parent_target is not None or child_target is not None:
                direction = "a_parent_b"
                endpoint_a, endpoint_b = parent_target, child_target
            else:
                direction = "pair"
                endpoint_a, endpoint_b = primary_target, secondary_target
            relationship = StructuralEdge(
                source=source,
                system_id=None if system_row is None else system_row.id,
                endpoint_a_target_id=None
                if endpoint_a is None
                else endpoint_a.id,
                endpoint_b_target_id=None
                if endpoint_b is None
                else endpoint_b.id,
                direction=direction,
                relation_type=relationship_type.strip(),
                component_label=component,
                separation_arcsec=separation_arcsec,
                pa_deg=pa_deg,
                relation_epoch=relation_epoch,
                confidence=confidence,
                status=_relationship_status(status),
                actor=actor,
                reason=reason,
            )
            session.add(relationship)
            session.flush()
            for target in {
                primary_target,
                secondary_target,
                parent_target,
                child_target,
            }:
                if target is not None:
                    mark_export_dirty(
                        session,
                        target.id,
                        source_type="hierarchy_relationship",
                        source_id=relationship.id,
                        reason="target relationship changed",
                    )
            return relationship

    def status(self, target_reference: str | int) -> HierarchyStatus:
        with self.session_factory() as session:
            target = _find_required_target(session, target_reference)
            systems = []
            rows = session.execute(
                select(TargetSystem, TargetSystemMember)
                .join(
                    TargetSystemMember,
                    TargetSystemMember.system_id == TargetSystem.id,
                )
                .where(TargetSystemMember.target_id == target.id)
                .order_by(TargetSystem.name, TargetSystemMember.id)
            )
            for system, member in rows:
                members = tuple(
                    SystemMember(
                        target_id=item_target.id,
                        sdbid=item_target.sdbid,
                        component_label=item_member.component_label,
                        source=item_member.source,
                    )
                    for item_member, item_target in session.execute(
                        select(TargetSystemMember, Target)
                        .join(Target, Target.id == TargetSystemMember.target_id)
                        .where(TargetSystemMember.system_id == system.id)
                        .order_by(TargetSystemMember.component_label, Target.id)
                    )
                )
                systems.append(
                    {
                        "id": system.id,
                        "name": system.name,
                        "source": system.source,
                        "note": system.note,
                        "primary_target_id": system.primary_target_id,
                        "component_label": member.component_label,
                        "members": tuple(asdict(item) for item in members),
                    }
                )
            relationships = tuple(
                _relationship_summary(session, value)
                for value in session.scalars(
                    select(StructuralEdge)
                    .where(StructuralEdge.status == RELATIONSHIP_STATUS)
                    .where(
                        or_(
                            StructuralEdge.endpoint_a_target_id == target.id,
                            StructuralEdge.endpoint_b_target_id == target.id,
                        )
                    )
                    .order_by(StructuralEdge.id)
                )
            )
            return HierarchyStatus(
                target_id=target.id,
                sdbid=target.sdbid,
                systems=tuple(systems),
                relationships=relationships,
            )


def _relationship_status(status: str) -> str:
    clean = status.strip()
    return RELATIONSHIP_STATUS if clean in ("", "current") else clean


def _find_required_target(
    session: Session,
    reference: str | int | None,
) -> Target:
    if reference is None:
        raise ValueError("target reference is required")
    target = resolve_target(session, reference)
    if target is None:
        raise KeyError(f"target not found: {reference}")
    return target


def _find_required_system(session: Session, name: str | None) -> TargetSystem:
    if name is None or not name.strip():
        raise ValueError("system name is required")
    system = session.scalar(
        select(TargetSystem).where(TargetSystem.name == name.strip())
    )
    if system is None:
        raise KeyError(f"system not found: {name}")
    return system


def _target_sdbid(session: Session, target_id: int | None) -> str | None:
    if target_id is None:
        return None
    target = session.get(Target, target_id)
    return None if target is None else target.sdbid


def _relationship_summary(
    session: Session,
    value: StructuralEdge,
) -> RelationshipSummary:
    parent_id = child_id = primary_id = secondary_id = None
    if value.direction == "a_parent_b":
        parent_id, child_id = value.endpoint_a_target_id, value.endpoint_b_target_id
    elif value.direction == "b_parent_a":
        parent_id, child_id = value.endpoint_b_target_id, value.endpoint_a_target_id
    else:
        primary_id, secondary_id = (
            value.endpoint_a_target_id,
            value.endpoint_b_target_id,
        )
    return RelationshipSummary(
        id=value.id,
        relationship_type=value.relation_type,
        source=value.source,
        status=value.status,
        confidence=value.confidence,
        component=value.component_label,
        parent_sdbid=_target_sdbid(session, parent_id),
        child_sdbid=_target_sdbid(session, child_id),
        primary_sdbid=_target_sdbid(session, primary_id),
        secondary_sdbid=_target_sdbid(session, secondary_id),
        separation_arcsec=value.separation_arcsec,
        pa_deg=value.pa_deg,
        relation_epoch=value.relation_epoch,
        reason=value.reason,
        actor=value.actor,
    )
