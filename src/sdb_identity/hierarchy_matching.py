"""Hierarchy-record candidate matching and audited match decisions."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from .decisions import DecisionContext
from .dirty import mark_export_dirty
from .hierarchy_geometry import (
    best_separation,
    position_usable_for_matching,
    record_positions,
    record_raw_payload,
    separation_arcsec,
    wds_record_has_unusable_separation,
)
from .identifiers import normalize_identifier
from .models import (
    ExternalIdentifier,
    HierarchyMatchAction,
    HierarchyMatchCandidate,
    HierarchyRecord,
    StructuralEdge,
    Target,
    TargetSystem,
    TargetSystemMember,
)
from .targets import resolve_target


@dataclass(frozen=True)
class HierarchyMatchResult:
    provider: str
    source_id: int | None
    record_count: int
    candidate_count: int
    radius_arcsec: float


@dataclass(frozen=True)
class HierarchyTargetMatchResult:
    provider: str
    target_count: int
    record_count: int
    candidate_count: int
    created_count: int
    updated_count: int
    radius_arcsec: float


@dataclass(frozen=True)
class HierarchyMatchReviewRow:
    candidate_id: int
    provider: str
    status: str
    record_id: int
    native_id: str
    component: str | None
    discoverer_id: str | None
    target_id: int
    sdbid: str
    match_method: str
    score: float
    separation_arcsec: float | None
    identifier: str | None
    reason: str


@dataclass(frozen=True)
class HierarchyMatchActionResult:
    candidate_id: int
    action: str
    previous_status: str
    new_status: str
    target_id: int
    sdbid: str
    system_id: int | None
    relationship_id: int | None


@dataclass
class _CandidateEvidence:
    methods: set[str] = field(default_factory=set)
    score: float = 0.0
    separation_arcsec: float | None = None
    identifier: str | None = None
    reasons: set[str] = field(default_factory=set)


class HierarchyMatchingService:
    """Generate, review, and decide hierarchy-record target candidates."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def match_records(
        self,
        provider: str,
        *,
        source_id: int | None = None,
        radius_arcsec: float = 30.0,
    ) -> HierarchyMatchResult:
        provider = _validated_provider(provider)
        _validate_radius(radius_arcsec)
        with self.session_factory.begin() as session:
            query = select(HierarchyRecord).where(
                HierarchyRecord.provider == provider,
            )
            if source_id is not None:
                query = query.where(HierarchyRecord.source_id == source_id)
            records = tuple(session.scalars(query.order_by(HierarchyRecord.id)))
            if records:
                session.execute(
                    delete(HierarchyMatchCandidate).where(
                        HierarchyMatchCandidate.record_id.in_(
                            [record.id for record in records],
                        ),
                    ),
                )
            alias_index = _build_alias_index(session)
            target_index = _build_target_index(
                tuple(session.scalars(select(Target))),
            )
            components_by_system = _components_by_system(records)
            candidate_count = 0
            for record in records:
                candidates = _candidates_for_record(
                    record,
                    radius_arcsec,
                    alias_index,
                    target_index,
                    components_by_system=components_by_system,
                )
                for target_id, evidence in candidates.items():
                    session.add(_candidate_model(
                        record, target_id, provider, evidence,
                    ))
                    candidate_count += 1
            return HierarchyMatchResult(
                provider=provider,
                source_id=source_id,
                record_count=len(records),
                candidate_count=candidate_count,
                radius_arcsec=radius_arcsec,
            )

    def match_targets(
        self,
        provider: str,
        target_references: Iterable[str | int],
        *,
        radius_arcsec: float = 30.0,
    ) -> HierarchyTargetMatchResult:
        """Refresh candidates for selected targets without touching others."""
        provider = _validated_provider(provider)
        _validate_radius(radius_arcsec)
        with self.session_factory.begin() as session:
            targets = _resolve_unique_targets(session, target_references)
            records = tuple(session.scalars(
                select(HierarchyRecord)
                .where(HierarchyRecord.provider == provider)
                .order_by(HierarchyRecord.id),
            ))
            if not targets or not records:
                return HierarchyTargetMatchResult(
                    provider=provider,
                    target_count=len(targets),
                    record_count=len(records),
                    candidate_count=0,
                    created_count=0,
                    updated_count=0,
                    radius_arcsec=radius_arcsec,
                )
            alias_index = _build_alias_index_for_targets(session, targets)
            target_index = _build_target_index(tuple(targets))
            components_by_system = _components_by_system(records)
            existing = {
                (candidate.record_id, candidate.target_id): candidate
                for candidate in session.scalars(
                    select(HierarchyMatchCandidate).where(
                        HierarchyMatchCandidate.provider == provider,
                        HierarchyMatchCandidate.target_id.in_(
                            [target.id for target in targets],
                        ),
                    ),
                )
            }
            candidate_count = created_count = updated_count = 0
            for record in records:
                candidates = _candidates_for_record(
                    record,
                    radius_arcsec,
                    alias_index,
                    target_index,
                    components_by_system=components_by_system,
                )
                for target_id, evidence in candidates.items():
                    candidate_count += 1
                    row = existing.get((record.id, target_id))
                    if row is None:
                        session.add(_candidate_model(
                            record, target_id, provider, evidence,
                        ))
                        created_count += 1
                    else:
                        _copy_candidate_evidence(row, evidence)
                        updated_count += 1
            return HierarchyTargetMatchResult(
                provider=provider,
                target_count=len(targets),
                record_count=len(records),
                candidate_count=candidate_count,
                created_count=created_count,
                updated_count=updated_count,
                radius_arcsec=radius_arcsec,
            )

    def review_matches(
        self, provider: str | None = None,
    ) -> tuple[HierarchyMatchReviewRow, ...]:
        with self.session_factory() as session:
            query = (
                select(HierarchyMatchCandidate, HierarchyRecord, Target)
                .join(
                    HierarchyRecord,
                    HierarchyRecord.id == HierarchyMatchCandidate.record_id,
                )
                .join(Target, Target.id == HierarchyMatchCandidate.target_id)
                .order_by(
                    HierarchyMatchCandidate.provider,
                    HierarchyRecord.native_id,
                    HierarchyRecord.component,
                    HierarchyMatchCandidate.score.desc(),
                    HierarchyMatchCandidate.separation_arcsec,
                    Target.sdbid,
                )
            )
            if provider is not None:
                query = query.where(
                    HierarchyMatchCandidate.provider == provider.lower().strip(),
                )
            return tuple(
                HierarchyMatchReviewRow(
                    candidate_id=candidate.id,
                    provider=candidate.provider,
                    status=candidate.status,
                    record_id=record.id,
                    native_id=record.native_id,
                    component=record.component,
                    discoverer_id=record.discoverer_id,
                    target_id=target.id,
                    sdbid=target.sdbid,
                    match_method=candidate.match_method,
                    score=candidate.score,
                    separation_arcsec=candidate.separation_arcsec,
                    identifier=candidate.identifier,
                    reason=candidate.reason,
                )
                for candidate, record, target in session.execute(query)
            )

    def accept_match(
        self,
        candidate_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
        system: str | None = None,
        component_label: str | None = None,
        relationship_type: str = "hierarchy_record",
    ) -> HierarchyMatchActionResult:
        if not relationship_type.strip():
            raise ValueError("relationship type is required")
        with self.session_factory.begin() as session:
            candidate, record, target = _required_candidate_context(
                session, candidate_id,
            )
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Accepted {record.provider} hierarchy candidate {candidate.id} "
                    f"for {target.sdbid}: {candidate.reason}"
                ),
            )
            previous_status = candidate.status
            system_row = None
            if system is not None:
                system_row = _find_or_create_system(
                    session,
                    system,
                    primary_target_id=target.id,
                    source=record.provider,
                    note=f"created from hierarchy candidate {candidate.id}",
                )
                _ensure_system_member(
                    session,
                    system_row.id,
                    target.id,
                    component_label if component_label is not None else record.component,
                    record.provider,
                )
            relationship = StructuralEdge(
                source=record.provider,
                system_id=None if system_row is None else system_row.id,
                record_id=record.id,
                native_id=record.native_id,
                endpoint_a_target_id=target.id,
                direction="pair",
                relation_type=relationship_type.strip(),
                component_label=record.component,
                separation_arcsec=record.separation_arcsec,
                pa_deg=record.pa_deg,
                relation_epoch=record.measure_epoch,
                confidence="accepted_candidate",
                status="accepted",
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(relationship)
            session.flush()
            candidate.status = "accepted"
            session.add(HierarchyMatchAction(
                candidate_id=candidate.id,
                action="accept",
                previous_status=previous_status,
                new_status=candidate.status,
                actor=decision.actor,
                reason=decision.reason,
                system_id=None if system_row is None else system_row.id,
                relationship_id=relationship.id,
            ))
            mark_export_dirty(
                session,
                target.id,
                source_type="hierarchy_match_accept",
                source_id=candidate.id,
                reason="hierarchy match accepted",
            )
            return HierarchyMatchActionResult(
                candidate_id=candidate.id,
                action="accept",
                previous_status=previous_status,
                new_status=candidate.status,
                target_id=target.id,
                sdbid=target.sdbid,
                system_id=None if system_row is None else system_row.id,
                relationship_id=relationship.id,
            )

    def reject_match(
        self,
        candidate_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> HierarchyMatchActionResult:
        with self.session_factory.begin() as session:
            candidate, record, target = _required_candidate_context(
                session, candidate_id,
            )
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Rejected {record.provider} hierarchy candidate {candidate.id} "
                    f"for {target.sdbid}"
                ),
            )
            previous_status = candidate.status
            candidate.status = "rejected"
            session.add(HierarchyMatchAction(
                candidate_id=candidate.id,
                action="reject",
                previous_status=previous_status,
                new_status=candidate.status,
                actor=decision.actor,
                reason=decision.reason,
            ))
            mark_export_dirty(
                session,
                target.id,
                source_type="hierarchy_match_reject",
                source_id=candidate.id,
                reason="hierarchy match rejected",
            )
            return HierarchyMatchActionResult(
                candidate_id=candidate.id,
                action="reject",
                previous_status=previous_status,
                new_status=candidate.status,
                target_id=target.id,
                sdbid=target.sdbid,
                system_id=None,
                relationship_id=None,
            )


def _validated_provider(provider: str) -> str:
    value = provider.lower().strip()
    if value not in {"wds", "ccdm"}:
        raise ValueError(f"unsupported hierarchy provider: {value}")
    return value


def _validate_radius(radius_arcsec: float) -> None:
    if radius_arcsec <= 0:
        raise ValueError("radius must be positive")


def _resolve_unique_targets(
    session: Session, target_references: Iterable[str | int],
) -> list[Target]:
    targets: list[Target] = []
    seen_target_ids: set[int] = set()
    for reference in target_references:
        target = _required_target(session, reference)
        if target.id not in seen_target_ids:
            targets.append(target)
            seen_target_ids.add(target.id)
    return targets


def _candidate_model(
    record: HierarchyRecord,
    target_id: int,
    provider: str,
    evidence: _CandidateEvidence,
) -> HierarchyMatchCandidate:
    row = HierarchyMatchCandidate(
        record_id=record.id,
        target_id=target_id,
        provider=provider,
    )
    _copy_candidate_evidence(row, evidence)
    return row


def _copy_candidate_evidence(
    row: HierarchyMatchCandidate, evidence: _CandidateEvidence,
) -> None:
    row.match_method = "+".join(sorted(evidence.methods))
    row.score = float(evidence.score)
    row.separation_arcsec = evidence.separation_arcsec
    row.identifier = evidence.identifier
    row.reason = "; ".join(sorted(evidence.reasons))


def _candidates_for_record(
    record: HierarchyRecord,
    radius_arcsec: float,
    alias_index: dict[str, tuple[tuple[str, Target], ...]],
    target_index: dict[int, tuple[Target, ...]],
    *,
    components_by_system: dict[tuple[str, int, str], frozenset[str]] | None = None,
) -> dict[int, _CandidateEvidence]:
    raw_payload = record_raw_payload(record)
    if wds_record_has_unusable_separation(record, raw_payload=raw_payload):
        return {}
    candidates: dict[int, _CandidateEvidence] = {}
    for identifier in _identifier_variants(
        record, components_by_system=components_by_system,
    ):
        normalized = normalize_identifier(identifier)
        if not normalized:
            continue
        for alias_value, target in alias_index.get(normalized, ()):
            evidence = candidates.setdefault(target.id, _CandidateEvidence())
            evidence.methods.add("identifier")
            evidence.score = max(evidence.score, 1.0)
            if evidence.identifier is None:
                evidence.identifier = alias_value
            evidence.reasons.add(f"identifier match: {alias_value}")
            separations = _target_separations(record, target, raw_payload=raw_payload)
            if separations:
                separation, position_kind = min(separations, key=lambda item: item[0])
                evidence.separation_arcsec = best_separation(
                    evidence.separation_arcsec, separation,
                )
                if separation <= radius_arcsec:
                    evidence.methods.add("position")
                    evidence.reasons.add(
                        f"{position_kind} separation {separation:.3f} arcsec",
                    )
                else:
                    evidence.reasons.add(
                        f"{position_kind} offset {separation:.3f} arcsec",
                    )
    if record_positions(record, raw_payload=raw_payload):
        for target, separation, position_kind in _targets_near_record(
            record, radius_arcsec, target_index, raw_payload=raw_payload,
        ):
            evidence = candidates.setdefault(target.id, _CandidateEvidence())
            evidence.methods.add("position")
            evidence.score = max(
                evidence.score,
                max(0.0, 0.95 * (1.0 - separation / radius_arcsec)),
            )
            evidence.separation_arcsec = best_separation(
                evidence.separation_arcsec, separation,
            )
            evidence.reasons.add(
                f"{position_kind} separation {separation:.3f} arcsec",
            )
    return candidates


def _build_alias_index(
    session: Session,
) -> dict[str, tuple[tuple[str, Target], ...]]:
    values: dict[str, list[tuple[str, Target]]] = {}
    rows = session.execute(
        select(ExternalIdentifier, Target)
        .join(Target, Target.id == ExternalIdentifier.target_id)
        .order_by(Target.sdbid, ExternalIdentifier.value),
    )
    for alias, target in rows:
        values.setdefault(alias.normalized_value, []).append((alias.value, target))
    return {key: tuple(value) for key, value in values.items()}


def _build_alias_index_for_targets(
    session: Session,
    targets: Iterable[Target],
) -> dict[str, tuple[tuple[str, Target], ...]]:
    values: dict[str, list[tuple[str, Target]]] = {}
    target_by_id = {target.id: target for target in targets}
    if not target_by_id:
        return {}
    rows = session.scalars(
        select(ExternalIdentifier)
        .where(ExternalIdentifier.target_id.in_(target_by_id))
        .order_by(ExternalIdentifier.target_id, ExternalIdentifier.value),
    )
    for alias in rows:
        values.setdefault(alias.normalized_value, []).append(
            (alias.value, target_by_id[alias.target_id]),
        )
    return {key: tuple(value) for key, value in values.items()}


def _build_target_index(
    targets: tuple[Target, ...],
) -> dict[int, tuple[Target, ...]]:
    values: dict[int, list[Target]] = {}
    for target in targets:
        values.setdefault(math.floor(target.dec2000_deg), []).append(target)
    return {
        key: tuple(sorted(value, key=lambda target: target.sdbid))
        for key, value in values.items()
    }


def _components_by_system(
    records: tuple[HierarchyRecord, ...],
) -> dict[tuple[str, int, str], frozenset[str]]:
    values: dict[tuple[str, int, str], set[str]] = {}
    for record in records:
        component = (record.component or "").strip().replace(" ", "")
        if record.native_id and component:
            values.setdefault(
                (record.provider, record.source_id, record.native_id), set(),
            ).add(component)
    return {key: frozenset(value) for key, value in values.items()}


def _identifier_variants(
    record: HierarchyRecord,
    *,
    components_by_system: dict[tuple[str, int, str], frozenset[str]] | None = None,
) -> tuple[str, ...]:
    values: list[str] = []
    native = (record.native_id or "").strip()
    discoverer = (record.discoverer_id or "").strip()
    component = (record.component or "").strip()
    if native:
        values.extend([native, f"{record.provider.upper()} {native}"])
        sibling_components = (components_by_system or {}).get(
            (record.provider, record.source_id, native), frozenset(),
        )
        component_variants = _component_identifier_variants(
            component, sibling_components=sibling_components,
        )
        if (
            record.provider == "wds"
            and not component_variants
            and _blank_wds_record_implies_ab_identifier(
                record, components_by_system=components_by_system,
            )
        ):
            component_variants = ("AB",)
        if record.provider == "wds":
            values.append(f"WDS J{native}")
            values.extend(
                f"WDS J{native}{variant}" for variant in component_variants
            )
        elif record.provider == "ccdm":
            coordinate_id = native[1:] if native.upper().startswith("J") else native
            values.extend([
                f"CCDM J{coordinate_id}",
                f"CCDM {coordinate_id}",
                f"WDS J{coordinate_id}",
            ])
            for variant in component_variants:
                values.extend([
                    f"CCDM J{coordinate_id}{variant}",
                    f"CCDM {coordinate_id}{variant}",
                    f"WDS J{coordinate_id}{variant}",
                ])
    if discoverer:
        discoverer_values = [discoverer, _spaced_designation(discoverer)]
        values.extend(discoverer_values)
        if component:
            compact_component = component.replace(" ", "")
            for value in discoverer_values:
                values.extend([
                    f"{value} {compact_component}", f"{value}{compact_component}",
                ])
    return tuple(dict.fromkeys(value for value in values if value.strip()))


def _blank_wds_record_implies_ab_identifier(
    record: HierarchyRecord,
    *,
    components_by_system: dict[tuple[str, int, str], frozenset[str]] | None,
) -> bool:
    if record.provider != "wds" or (record.component or "").strip():
        return False
    if not record.native_id:
        return False
    if record.separation_arcsec is None or record.pa_deg is None:
        return False
    if record.separation_arcsec <= 0:
        return False
    explicit = (components_by_system or {}).get(
        (record.provider, record.source_id, record.native_id), frozenset(),
    )
    return "AB" not in explicit


def _component_identifier_variants(
    component: str,
    *,
    sibling_components: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    compact = component.replace(" ", "")
    if not compact:
        return ()
    values = [compact]
    if "," not in compact and compact.isalpha() and compact.isupper() and len(compact) > 1:
        values.extend(compact)
    parent_component = _compound_component_parent(compact)
    if parent_component is not None:
        values.append(parent_component)
    if compact == "A":
        for sibling in sorted(sibling_components):
            if len(sibling) == 1 and sibling != "A":
                values.append(f"A{sibling}")
    if len(compact) == 1 and compact != "A":
        values.append(f"A{compact}")
    return tuple(dict.fromkeys(values))


def _compound_component_parent(component: str) -> str | None:
    parts = [part.strip() for part in component.split(",") if part.strip()]
    if len(parts) < 2:
        return None
    parents: set[str] = set()
    for part in parts:
        match = re.match(r"^([A-Z])[a-z0-9]+$", part)
        if match is None:
            return None
        parents.add(match.group(1))
    return next(iter(parents)) if len(parents) == 1 else None


def _targets_near_record(
    record: HierarchyRecord,
    radius_arcsec: float,
    target_index: dict[int, tuple[Target, ...]],
    *,
    raw_payload: dict[str, object] | None = None,
) -> tuple[tuple[Target, float, str], ...]:
    positions = tuple(
        position for position in record_positions(record, raw_payload=raw_payload)
        if position_usable_for_matching(position[2])
    )
    if not positions:
        return ()
    radius_deg = radius_arcsec / 3600.0
    rows: dict[int, tuple[Target, float, str]] = {}
    for ra_deg, dec_deg, position_kind in positions:
        cos_dec = max(0.01, abs(math.cos(math.radians(dec_deg))))
        ra_half_width = min(180.0, radius_deg / cos_dec)
        dec_min = dec_deg - radius_deg
        dec_max = dec_deg + radius_deg
        for dec_bin in range(math.floor(dec_min), math.floor(dec_max) + 1):
            for target in target_index.get(dec_bin, ()):
                if target.dec2000_deg < dec_min or target.dec2000_deg > dec_max:
                    continue
                if not _ra_within(ra_deg, target.ra2000_deg, ra_half_width):
                    continue
                separation = separation_arcsec(
                    ra_deg, dec_deg, target.ra2000_deg, target.dec2000_deg,
                )
                if separation <= radius_arcsec:
                    existing = rows.get(target.id)
                    if existing is None or separation < existing[1]:
                        rows[target.id] = (target, separation, position_kind)
    return tuple(sorted(rows.values(), key=lambda item: (item[1], item[0].sdbid)))


def _target_separations(
    record: HierarchyRecord,
    target: Target,
    *,
    raw_payload: dict[str, object] | None = None,
) -> tuple[tuple[float, str], ...]:
    return tuple(
        (
            separation_arcsec(
                ra_deg, dec_deg, target.ra2000_deg, target.dec2000_deg,
            ),
            position_kind,
        )
        for ra_deg, dec_deg, position_kind in record_positions(
            record, raw_payload=raw_payload,
        )
        if position_usable_for_matching(position_kind)
    )


def _ra_within(center_deg: float, value_deg: float, half_width_deg: float) -> bool:
    if half_width_deg >= 180:
        return True
    delta = abs((value_deg - center_deg + 180.0) % 360.0 - 180.0)
    return delta <= half_width_deg


def _spaced_designation(value: str) -> str:
    return re.sub(r"^([A-Za-z]+)(\d+)$", r"\1 \2", value.strip())


def _required_target(session: Session, reference: str | int) -> Target:
    target = resolve_target(session, reference)
    if target is None:
        raise KeyError(f"target not found: {reference}")
    return target


def _required_candidate_context(
    session: Session,
    candidate_id: int,
) -> tuple[HierarchyMatchCandidate, HierarchyRecord, Target]:
    row = session.execute(
        select(HierarchyMatchCandidate, HierarchyRecord, Target)
        .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
        .join(Target, Target.id == HierarchyMatchCandidate.target_id)
        .where(HierarchyMatchCandidate.id == candidate_id),
    ).one_or_none()
    if row is None:
        raise KeyError(f"hierarchy match candidate not found: {candidate_id}")
    candidate, record, target = row
    return candidate, record, target


def _find_or_create_system(
    session: Session,
    name: str,
    *,
    primary_target_id: int,
    source: str,
    note: str,
) -> TargetSystem:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("system name is required")
    system = session.scalar(
        select(TargetSystem).where(TargetSystem.name == clean_name),
    )
    if system is not None:
        return system
    system = TargetSystem(
        name=clean_name,
        primary_target_id=primary_target_id,
        source=source,
        note=note,
    )
    session.add(system)
    session.flush()
    return system


def _ensure_system_member(
    session: Session,
    system_id: int,
    target_id: int,
    component_label: str | None,
    source: str,
) -> TargetSystemMember:
    member = session.scalar(
        select(TargetSystemMember).where(
            TargetSystemMember.system_id == system_id,
            TargetSystemMember.target_id == target_id,
            TargetSystemMember.component_label == component_label,
        ),
    )
    if member is not None:
        return member
    member = TargetSystemMember(
        system_id=system_id,
        target_id=target_id,
        component_label=component_label,
        source=source,
    )
    session.add(member)
    return member
