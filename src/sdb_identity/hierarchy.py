from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import Integer, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import angular_separation_arcsec
from .adapters.vizier import row_payload
from .adapters.review_metadata import normalize_review_payload
from .cache_store import CachedSnapshotData, SnapshotCache
from .catalog_measurements import (
    current_measurement_encounters,
    current_measurements_for_target,
)
from .dirty import find_target, mark_export_dirty
from .decisions import DecisionContext
from .models import (
    AstrometricSolution,
    ExternalIdentifier,
    HierarchyMatchAction,
    HierarchyMatchCandidate,
    HierarchyRecord,
    HierarchySource,
    CatalogRun,
    MatchCandidate,
    MeasurementTargetAssociation,
    MetadataRun,
    NormalizedMeasurement,
    RawCatalogRow,
    SimbadMetadata,
    SimbadRelationship,
    StructuralEdge,
    StructuralEdgeAction,
    Submission,
    Target,
    TargetLifecycleAction,
    TargetSystem,
    TargetSystemMember,
    utcnow,
)
from .providers import Astrometry, ProviderError
from .service import normalize_identifier
from .snapshots import SnapshotClient, VizierSnapshotClient


HIERARCHY_CATALOGS = {
    "wds": "B/wds",
    "ccdm": "I/274",
}

HIERARCHY_MAIN_TABLES = {
    "wds": {"b/wds/wds", "b_wds_wds", "wds"},
    "ccdm": {"i/274/ccdm", "i_274_ccdm", "ccdm"},
}

# Structural edges hold both provider-derived graph edges and target-resolved
# relationships in one table. Graph readers see only re-derivable provider edges;
# relationship readers see only accepted assertions.
_GRAPH_EDGE_STATUSES = ("derived", "stale", "rejected")
_RELATIONSHIP_STATUS = "accepted"


def _relationship_status(status: str) -> str:
    """Normalize a relationship status, mapping the legacy default to accepted."""
    clean = status.strip()
    return _RELATIONSHIP_STATUS if clean in ("", "current") else clean

WDS_UNUSABLE_SEPARATION_ARCSEC = 999.8
_COMPONENT_TOKEN_RE = re.compile(r"^(?:[A-Z]{1,3}|[A-Z][a-z0-9])$")
_TRAILING_COMPONENT_RE = re.compile(r"(?:^|[\s_-])([A-Z]{1,3}|[A-Z][a-z0-9])$")
_WDS_CCDM_COMPONENT_RE = re.compile(r"\b(?:WDS|CCDM)\s+J?\d{4,6}[+-]\d{4,6}\s*([A-Z]{1,3}|[A-Z][a-z0-9])\b", re.IGNORECASE)
_HD_ATTACHED_COMPONENT_RE = re.compile(r"^HD\s+\d+([A-Z]{1,3}|[A-Z][a-z0-9])$")


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


@dataclass(frozen=True)
class HierarchyImportResult:
    source_id: int
    provider: str
    release: str
    row_count: int
    skipped_count: int
    checksum: str


@dataclass(frozen=True)
class HierarchyPruneResult:
    provider: str | None
    groups: int
    removed_sources: int
    removed_records: int
    removed_candidates: int
    removed_match_actions: int
    removed_graph_edges: int
    removed_graph_overrides: int


@dataclass(frozen=True)
class HierarchyMatchResult:
    provider: str
    source_id: int | None
    record_count: int
    candidate_count: int
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


@dataclass(frozen=True)
class HierarchyGraphDeriveResult:
    provider: str
    source_id: int | None
    record_count: int
    edge_count: int
    skipped_count: int


@dataclass(frozen=True)
class HierarchyGraphEdgeRow:
    edge_id: int
    source_id: int
    record_id: int | None
    provider: str
    native_id: str
    source_component: str | None
    reference_label: str | None
    component_label: str | None
    relation_type: str
    structural_role: str
    status: str
    geometry_status: str
    start_ra_deg: float | None
    start_dec_deg: float | None
    end_ra_deg: float | None
    end_dec_deg: float | None
    separation_arcsec: float | None
    pa_deg: float | None
    relation_epoch: float | None
    note: str
    override_id: int | None = None
    override_actor: str | None = None
    override_reason: str | None = None


@dataclass(frozen=True)
class HierarchyGraphOverrideResult:
    override_id: int
    edge_id: int
    previous_status: str
    new_status: str
    previous_relation_type: str
    new_relation_type: str
    previous_structural_role: str
    new_structural_role: str
    actor: str
    reason: str


@dataclass(frozen=True)
class HierarchyGraphDiagnosticRow:
    provider: str
    source_id: int
    native_id: str
    issue: str
    severity: str
    edge_count: int
    structural_count: int
    non_structural_count: int
    matched_candidate_count: int
    detail: str


@dataclass(frozen=True)
class ParsedHierarchyRecord:
    native_id: str
    component: str | None = None
    discoverer_id: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    first_epoch: float | None = None
    last_epoch: float | None = None
    measure_epoch: float | None = None
    separation_arcsec: float | None = None
    pa_deg: float | None = None
    magnitude_primary: float | None = None
    magnitude_secondary: float | None = None
    delta_mag: float | None = None
    raw_payload: dict[str, object] | None = None


class HierarchyService:
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
            existing = session.scalar(select(TargetSystem).where(TargetSystem.name == clean_name))
            if existing is not None:
                raise ValueError(f"system already exists: {clean_name}")
            primary_target = _find_required_target(session, primary) if primary is not None else None
            system = TargetSystem(
                name=clean_name,
                primary_target_id=None if primary_target is None else primary_target.id,
                source=source,
                note=note,
            )
            session.add(system)
            session.flush()
            if primary_target is not None:
                session.add(TargetSystemMember(
                    system_id=system.id,
                    target_id=primary_target.id,
                    component_label=None,
                    source=source,
                ))
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
            system_row = _find_required_system(session, system) if system is not None else None
            primary_target = _find_required_target(session, primary) if primary is not None else None
            secondary_target = _find_required_target(session, secondary) if secondary is not None else None
            parent_target = _find_required_target(session, parent) if parent is not None else None
            child_target = _find_required_target(session, child) if child is not None else None
            if primary_target is None and secondary_target is None and parent_target is None and child_target is None:
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
                endpoint_a_target_id=None if endpoint_a is None else endpoint_a.id,
                endpoint_b_target_id=None if endpoint_b is None else endpoint_b.id,
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
            for target in {primary_target, secondary_target, parent_target, child_target}:
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
                .join(TargetSystemMember, TargetSystemMember.system_id == TargetSystem.id)
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
                systems.append({
                    "id": system.id,
                    "name": system.name,
                    "source": system.source,
                    "note": system.note,
                    "primary_target_id": system.primary_target_id,
                    "component_label": member.component_label,
                    "members": tuple(asdict(item) for item in members),
                })
            relationships = tuple(
                _relationship_summary(session, value)
                for value in session.scalars(
                    select(StructuralEdge)
                    .where(StructuralEdge.status == _RELATIONSHIP_STATUS)
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

    def target_context(
        self,
        target_reference: str | int,
        *,
        include_diagnostics: bool = True,
    ) -> dict[str, object]:
        with self.session_factory() as session:
            target = _find_required_target(session, target_reference)
            semantic_identity = _target_semantic_identity(session, target)
            all_candidate_rows = tuple(session.execute(
                select(HierarchyMatchCandidate, HierarchyRecord)
                .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
                .where(HierarchyMatchCandidate.target_id == target.id)
                .order_by(
                    HierarchyRecord.provider,
                    HierarchyRecord.source_id,
                    HierarchyRecord.native_id,
                    HierarchyMatchCandidate.score.desc(),
                    HierarchyMatchCandidate.id,
                )
            ))
            accepted_candidate_rows = tuple(
                (candidate, record)
                for candidate, record in all_candidate_rows
                if candidate.status == "accepted"
            )
            if accepted_candidate_rows:
                candidate_rows = accepted_candidate_rows
                decision_basis = "accepted_candidates"
            else:
                candidate_rows = tuple(
                    (candidate, record)
                    for candidate, record in all_candidate_rows
                    if candidate.status == "candidate"
                )
                decision_basis = "candidate_review" if candidate_rows else "none"
            keys = sorted({
                (record.provider, record.source_id, record.native_id)
                for _candidate, record in candidate_rows
            })
            edge_rows: dict[tuple[str, int, str], list[HierarchyGraphEdgeRow]] = {}
            diagnostic_rows: dict[tuple[str, int, str], list[HierarchyGraphDiagnosticRow]] = {}
            for provider, source_id, native_id in keys:
                if provider == "wds":
                    edge_rows[(provider, source_id, native_id)] = list(_graph_edges_for_system(
                        session,
                        provider=provider,
                        native_id=native_id,
                        source_id=source_id,
                    ))
                    if include_diagnostics:
                        diagnostic_rows[(provider, source_id, native_id)] = [
                            row for row in self.graph_diagnostics(
                                provider=provider,
                                source_id=source_id,
                                native_id=native_id,
                                limit=0,
                            )
                        ]
                    else:
                        diagnostic_rows[(provider, source_id, native_id)] = []
                else:
                    edge_rows[(provider, source_id, native_id)] = []
                    diagnostic_rows[(provider, source_id, native_id)] = []

            target_position = Astrometry(target.ra2000_deg, target.dec2000_deg)
            systems = []
            all_components = []
            review_required = False
            for provider, source_id, native_id in keys:
                candidates = [
                    _target_context_candidate(candidate, record)
                    for candidate, record in candidate_rows
                    if (
                        record.provider == provider
                        and record.source_id == source_id
                        and record.native_id == native_id
                    )
                ]
                edges = edge_rows[(provider, source_id, native_id)]
                diagnostics = diagnostic_rows[(provider, source_id, native_id)]
                review_required = review_required or any(row.severity == "review" for row in diagnostics)
                components = _target_context_components(target_position, edges)
                all_components.extend(
                    {
                        **component,
                        "provider": provider,
                        "source_id": source_id,
                        "native_id": native_id,
                    }
                    for component in components
                )
                systems.append({
                    "provider": provider,
                    "source_id": source_id,
                    "native_id": native_id,
                    "candidates": candidates,
                    "components": components,
                    "edges": [asdict(edge) for edge in edges],
                    "diagnostics": [asdict(row) for row in diagnostics],
                })

            nearest = min(
                all_components,
                key=lambda component: component["separation_arcsec"],
                default=None,
            )
            closest_companion = None
            if nearest is not None:
                companions = [
                    component for component in all_components
                    if not (
                        component["provider"] == nearest["provider"]
                        and component["source_id"] == nearest["source_id"]
                        and component["native_id"] == nearest["native_id"]
                        and component["component"] == nearest["component"]
                    )
                ]
                for component in companions:
                    component["separation_from_nearest_arcsec"] = _position_separation_arcsec(
                        nearest["ra_deg"],
                        nearest["dec_deg"],
                        component["ra_deg"],
                        component["dec_deg"],
                    )
                closest_companion = min(
                    companions,
                    key=lambda component: component["separation_from_nearest_arcsec"],
                    default=None,
                )
            classification = _target_context_classification(
                systems=systems,
                nearest_component=nearest,
                closest_companion=closest_companion,
                review_required=review_required,
            )
            component_assignment = _target_component_assignment(
                semantic_identity=semantic_identity,
                nearest_component=nearest,
                closest_companion=closest_companion,
                systems=systems,
                review_required=review_required,
            )
            photometry_context = _target_photometry_context(
                session,
                target,
                component_assignment=component_assignment,
                closest_companion=closest_companion,
            )
            return {
                "target": {
                    "id": target.id,
                    "sdbid": target.sdbid,
                    "ra2000_deg": target.ra2000_deg,
                    "dec2000_deg": target.dec2000_deg,
                },
                "classification": classification,
                "review_required": review_required,
                "semantic_identity": semantic_identity,
                "component_assignment": component_assignment,
                "photometry_context": photometry_context,
                "matched_systems": len(systems),
                "hierarchy_decision_basis": decision_basis,
                "nearest_component": nearest,
                "closest_companion": closest_companion,
                "systems": systems,
            }

    def target_context_summary(self, target_reference: str | int) -> dict[str, object]:
        context = self.target_context(target_reference)
        warnings = []
        closest = context["closest_companion"]
        if closest is not None:
            warnings.append("known hierarchy component may affect low-resolution photometry")
        if context["review_required"]:
            warnings.append("hierarchy review diagnostics are present")
        return {
            "classification": context["classification"],
            "review_required": context["review_required"],
            "semantic_identity": _target_semantic_identity_summary(context["semantic_identity"]),
            "component_assignment": _target_component_assignment_summary(context["component_assignment"]),
            "photometry_context": _target_photometry_context_summary(context["photometry_context"]),
            "matched_systems": context["matched_systems"],
            "hierarchy_decision_basis": context["hierarchy_decision_basis"],
            "nearest_component": context["nearest_component"],
            "nearby_components": sum(len(system["components"]) for system in context["systems"]),
            "closest_companion": closest,
            "warnings": warnings,
        }

    def system_context(self, target_reference: str | int) -> dict[str, object]:
        """Return a read-only, system-level review context for one target.

        This deliberately composes existing target-level evidence rather than
        persisting a new system interpretation. It is intended to make cases
        such as A/B components, rejected sibling Gaia candidates, and blended
        photometry visible before we decide what should become auditable state.
        """
        target_context = self.target_context(target_reference, include_diagnostics=True)
        with self.session_factory() as session:
            target = _find_required_target(session, target_reference)
            system_keys = _target_context_system_keys(target_context)
            component_positions = _system_component_positions(target_context)
            radius_arcsec = _system_context_radius_arcsec(component_positions)
            nearby_targets = _nearby_sdb_targets(
                session,
                target,
                radius_arcsec=radius_arcsec,
            )
            explicit_target_ids = _explicit_system_target_ids(session, target.id)
            present_target_ids = {int(row["target_id"]) for row in nearby_targets}
            nearby_targets.extend(
                _system_target_review_row(session, target, member)
                for member in session.scalars(select(Target).where(
                    Target.id.in_(explicit_target_ids - present_target_ids)
                ))
            )
            nearby_targets.sort(key=lambda row: float(row["separation_arcsec"]))
            target_ids = sorted({
                target.id,
                *explicit_target_ids,
                *(int(row["target_id"]) for row in nearby_targets),
            })
            hierarchy_candidates = _system_hierarchy_candidates(
                session,
                target_ids,
                system_keys=system_keys,
            )
            component_positions = _annotate_system_component_targets(
                component_positions,
                nearby_targets=nearby_targets,
                hierarchy_candidates_by_target=hierarchy_candidates,
                requested_sdbid=target.sdbid,
            )
            photometry = _system_photometry(session, target_ids)
            measurement_assignments = _system_measurement_assignments(session, target_ids)
            target_lifecycle = _system_target_lifecycle(session, target_ids)
            system_memberships = _system_memberships(session, target_ids)
            simbad_metadata = _system_simbad_metadata(session, target_ids)
            simbad_main_ids = _system_simbad_main_ids(
                session, target_ids, metadata_by_target=simbad_metadata,
            )
            catalog_neighbourhood = _system_catalog_neighbourhood(session, target_ids)
            identity_cross_candidates = _identity_cross_candidates(
                session,
                target,
                nearby_target_ids=set(target_ids),
            )
            semantic = {
                row["sdbid"]: _target_semantic_identity_summary(
                    _target_semantic_identity(session, session.get(Target, int(row["target_id"])))
                )
                for row in nearby_targets
                if session.get(Target, int(row["target_id"])) is not None
            }
            result = {
                "target": target_context["target"],
                "radius_arcsec": radius_arcsec,
                "target_context": {
                    "classification": target_context["classification"],
                    "hierarchy_decision_basis": target_context["hierarchy_decision_basis"],
                    "component_assignment": target_context["component_assignment"],
                    "photometry_context": _target_photometry_context_summary(
                        target_context["photometry_context"]
                    ),
                    "nearest_component": target_context["nearest_component"],
                    "closest_companion": target_context["closest_companion"],
                },
                "systems": target_context["systems"],
                "component_positions": component_positions,
                "nearby_sdb_targets": nearby_targets,
                "simbad_semantic_by_target": semantic,
                "simbad_metadata_by_target": simbad_metadata,
                "simbad_main_id_by_target": simbad_main_ids,
                "hierarchy_candidates_by_target": hierarchy_candidates,
                "identity_cross_candidates": identity_cross_candidates,
                "photometry_by_target": photometry,
                "measurement_assignments": measurement_assignments,
                "target_lifecycle_by_target": target_lifecycle,
                "system_memberships_by_target": system_memberships,
                "catalog_neighbourhood_by_target": catalog_neighbourhood,
                "notes": [
                    "read-only review context; no system/export decisions are persisted",
                    "identity_cross_candidates show rejected/accepted source candidates that resolve to another nearby SDB target",
                ],
            }
        from .system_expansion import preview_immediate_relatives

        try:
            result["simbad_relative_preview"] = preview_immediate_relatives(
                self.session_factory, target_reference,
            )
        except ValueError as error:
            result["simbad_relative_preview"] = []
            result["notes"].append(str(error))
        return result

    def photometry_review(
        self,
        target_references: list[str | int],
        *,
        provider: str | None = None,
        blended_only: bool = False,
        review_required: bool = False,
    ) -> list[dict[str, object]]:
        if blended_only:
            target_references = self._references_with_current_measurements(
                target_references,
                provider=provider,
            )
        rows = []
        for reference in target_references:
            context = self.target_context(reference, include_diagnostics=False)
            photometry = dict(context["photometry_context"])
            if provider:
                bands = [
                    band for band in photometry["bands"]
                    if band["provider"] == provider
                ]
                photometry["bands"] = bands
                photometry["measurement_count"] = len(bands)
                photometry["likely_blended_bands"] = [
                    value for value in photometry["likely_blended_bands"]
                    if value.startswith(f"{provider}:")
                ]
                _refresh_photometry_band_summaries(photometry)
            if blended_only and not photometry["likely_blended_bands"]:
                continue
            if review_required and not photometry["review_required"]:
                continue
            assignment = context["component_assignment"]
            semantic = context["semantic_identity"]
            rows.append({
                "sdbid": context["target"]["sdbid"],
                "classification": context["classification"],
                "semantic_kind": semantic["kind"],
                "semantic_confidence": semantic["confidence"],
                "component_assignment_status": assignment["status"],
                "component_assignment_confidence": assignment["confidence"],
                "hierarchy_decision_basis": context["hierarchy_decision_basis"],
                "target_level": photometry["target_level"],
                "nearest_pair_arcsec": photometry["nearest_pair_arcsec"],
                "likely_unresolved_components": photometry["likely_unresolved_components"],
                "measurement_count": photometry["measurement_count"],
                "likely_blended_bands": photometry["likely_blended_bands"],
                "predicted_scope_counts": photometry["predicted_scope_counts"],
                "predicted_blend_counts": photometry["predicted_blend_counts"],
                "bands": photometry["bands"],
                "recommendation": photometry["recommendation"],
                "review_required": photometry["review_required"],
            })
        return rows

    def review_queue(
        self,
        target_references: list[str | int],
        *,
        provider: str | None = None,
        min_priority: str | None = None,
    ) -> list[dict[str, object]]:
        provider_value = None if provider is None else provider.lower().strip()
        min_rank = 0 if min_priority is None else _review_priority_rank(min_priority)
        rows = []
        for reference in target_references:
            context = self.target_context(reference, include_diagnostics=True)
            photometry = dict(context["photometry_context"])
            if provider_value:
                bands = [
                    band for band in photometry["bands"]
                    if band["provider"] == provider_value
                ]
                photometry["bands"] = bands
                photometry["measurement_count"] = len(bands)
                photometry["likely_blended_bands"] = [
                    value for value in photometry["likely_blended_bands"]
                    if value.startswith(f"{provider_value}:")
                ]
                _refresh_photometry_band_summaries(photometry)
            row = _review_queue_row(context, photometry)
            if _review_priority_rank(str(row["priority"])) < min_rank:
                continue
            rows.append(row)
        return sorted(
            rows,
            key=lambda row: (
                -_review_priority_rank(str(row["priority"])),
                str(row["sdbid"]),
            ),
        )

    def _references_with_current_measurements(
        self,
        target_references: list[str | int],
        *,
        provider: str | None,
    ) -> list[str | int]:
        filtered = []
        with self.session_factory() as session:
            for reference in target_references:
                target = _find_required_target(session, reference)
                measurements = current_measurements_for_target(session, target.id)
                if any(not provider or value.provider == provider for value in measurements):
                    filtered.append(reference)
        return filtered

    def import_snapshot(
        self,
        provider: str,
        path: str | Path,
        *,
        release: str,
        note: str | None = None,
    ) -> HierarchyImportResult:
        provider = provider.lower().strip()
        if provider not in {"wds", "ccdm"}:
            raise ValueError(f"unsupported hierarchy provider: {provider}")
        if not release.strip():
            raise ValueError("release is required")
        path = Path(path).expanduser().resolve()
        data = path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        text_data = gzip.decompress(data) if path.suffix == ".gz" else data
        text = text_data.decode("utf-8", errors="replace")
        parsed, skipped = _parse_hierarchy_snapshot(provider, text)
        with self.session_factory.begin() as session:
            existing = self._existing_source_result(
                session, provider, checksum, skipped_count=skipped,
            )
            if existing is not None:
                return existing
            source = HierarchySource(
                provider=provider,
                release=release.strip(),
                source_file=str(path),
                checksum=checksum,
                note=note,
            )
            session.add(source)
            session.flush()
            for record in parsed:
                session.add(HierarchyRecord(
                    source_id=source.id,
                    provider=provider,
                    native_id=record.native_id,
                    component=record.component,
                    discoverer_id=record.discoverer_id,
                    ra_deg=record.ra_deg,
                    dec_deg=record.dec_deg,
                    first_epoch=record.first_epoch,
                    last_epoch=record.last_epoch,
                    measure_epoch=record.measure_epoch,
                    separation_arcsec=record.separation_arcsec,
                    pa_deg=record.pa_deg,
                    magnitude_primary=record.magnitude_primary,
                    magnitude_secondary=record.magnitude_secondary,
                    delta_mag=record.delta_mag,
                    raw_payload_json=json.dumps(record.raw_payload or {}, sort_keys=True),
                ))
            return HierarchyImportResult(
                source_id=source.id,
                provider=provider,
                release=source.release,
                row_count=len(parsed),
                skipped_count=skipped,
                checksum=checksum,
            )

    def fetch_snapshot(
        self,
        provider: str,
        *,
        client: SnapshotClient | None = None,
        cache_path: str | Path | None = None,
        refresh_cache: bool = False,
        release: str | None = None,
        note: str | None = None,
    ) -> HierarchyImportResult:
        provider = provider.lower().strip()
        catalog = HIERARCHY_CATALOGS.get(provider)
        if catalog is None:
            raise ValueError(f"unsupported hierarchy provider: {provider}")
        client = client or VizierSnapshotClient()
        cache_status = "disabled"
        if cache_path is not None:
            cache = SnapshotCache(cache_path)
            cached = None if refresh_cache else cache.current_snapshot("vizier", catalog)
            if cached is None:
                try:
                    tables = client.fetch_tables(catalog)
                    readme = client.fetch_readme(catalog)
                except Exception as error:
                    raise ProviderError(
                        f"{provider} hierarchy snapshot fetch failed: {error}",
                        transient=True,
                    ) from error
                cached = cache.store_snapshot(
                    provider="vizier",
                    catalog_id=catalog,
                    release=release or _release_from_readme(provider, catalog, readme),
                    source_url=client.source_url(catalog),
                    readme=readme,
                    tables=tables,
                    note=note,
                )
                cache_status = "stored"
            else:
                cache_status = "reused"
            parsed = _parse_cached_hierarchy_snapshot(provider, cached)
            readme = cached.readme
            source_url = cached.source_url
            checksum = cached.content_sha256
            release_value = release or cached.release
            cache_note = f"cache_source_id={cached.source_id};cache_status={cache_status}"
        else:
            try:
                tables = client.fetch_tables(catalog)
                readme = client.fetch_readme(catalog)
            except Exception as error:
                raise ProviderError(
                    f"{provider} hierarchy snapshot fetch failed: {error}", transient=True
                ) from error
            parsed = _parse_hierarchy_tables(provider, tables)
            if not parsed:
                raise ProviderError(f"{provider} hierarchy snapshot returned no parseable rows")
            release_value = release or _release_from_readme(provider, catalog, readme)
            canonical = json.dumps({
                "catalog": catalog,
                "provider": provider,
                "readme": readme,
                "records": [asdict(record) for record in parsed],
            }, sort_keys=True, ensure_ascii=False)
            checksum = hashlib.sha256(canonical.encode()).hexdigest()
            source_url = client.source_url(catalog)
            cache_note = None
        if not parsed:
            raise ProviderError(f"{provider} hierarchy snapshot returned no parseable rows")
        with self.session_factory.begin() as session:
            existing = self._existing_source_result(
                session, provider, checksum, skipped_count=0,
            )
            if existing is not None:
                return existing
            source = HierarchySource(
                provider=provider,
                release=release_value,
                source_file=source_url,
                checksum=checksum,
                fetched_at=utcnow(),
                note=_join_notes(note, _readme_version_note(readme), cache_note),
            )
            session.add(source)
            session.flush()
            for record in parsed:
                session.add(HierarchyRecord(
                    source_id=source.id,
                    provider=provider,
                    native_id=record.native_id,
                    component=record.component,
                    discoverer_id=record.discoverer_id,
                    ra_deg=record.ra_deg,
                    dec_deg=record.dec_deg,
                    first_epoch=record.first_epoch,
                    last_epoch=record.last_epoch,
                    measure_epoch=record.measure_epoch,
                    separation_arcsec=record.separation_arcsec,
                    pa_deg=record.pa_deg,
                    magnitude_primary=record.magnitude_primary,
                    magnitude_secondary=record.magnitude_secondary,
                    delta_mag=record.delta_mag,
                    raw_payload_json=json.dumps(record.raw_payload or {}, sort_keys=True),
                ))
            return HierarchyImportResult(
                source_id=source.id,
                provider=provider,
                release=source.release,
                row_count=len(parsed),
                skipped_count=0,
                checksum=checksum,
            )

    def _existing_source_result(
        self,
        session: Session,
        provider: str,
        checksum: str | None,
        *,
        skipped_count: int,
    ) -> HierarchyImportResult | None:
        if checksum is None:
            return None
        source = session.scalar(
            select(HierarchySource)
            .where(
                HierarchySource.provider == provider,
                HierarchySource.checksum == checksum,
            )
            .order_by(HierarchySource.id)
            .limit(1)
        )
        if source is None:
            return None
        row_count = session.scalar(
            select(func.count(HierarchyRecord.id))
            .where(HierarchyRecord.source_id == source.id)
        ) or 0
        return HierarchyImportResult(
            source_id=source.id,
            provider=source.provider,
            release=source.release,
            row_count=int(row_count),
            skipped_count=skipped_count,
            checksum=checksum,
        )

    def sources(self, provider: str | None = None) -> tuple[HierarchySource, ...]:
        with self.session_factory() as session:
            query = select(HierarchySource).order_by(HierarchySource.id)
            if provider is not None:
                query = query.where(HierarchySource.provider == provider.lower().strip())
            return tuple(session.scalars(query))

    def prune_duplicate_sources(self, provider: str | None = None) -> HierarchyPruneResult:
        provider_value = None if provider is None else provider.lower().strip()
        if provider_value is not None and provider_value not in {"wds", "ccdm", "simbad", "manual"}:
            raise ValueError(f"unsupported hierarchy provider: {provider}")
        with self.session_factory.begin() as session:
            group_query = (
                select(HierarchySource.provider, HierarchySource.checksum)
                .where(HierarchySource.checksum.is_not(None))
                .group_by(HierarchySource.provider, HierarchySource.checksum)
                .having(func.count(HierarchySource.id) > 1)
                .order_by(HierarchySource.provider, HierarchySource.checksum)
            )
            if provider_value is not None:
                group_query = group_query.where(HierarchySource.provider == provider_value)
            groups = list(session.execute(group_query))
            remove_source_ids: list[int] = []
            for group_provider, checksum in groups:
                sources = list(session.scalars(
                    select(HierarchySource)
                    .where(
                        HierarchySource.provider == group_provider,
                        HierarchySource.checksum == checksum,
                    )
                    .order_by(HierarchySource.id)
                ))
                remove_source_ids.extend(source.id for source in sources[1:])
            if not remove_source_ids:
                return HierarchyPruneResult(provider_value, 0, 0, 0, 0, 0, 0, 0)

            record_ids = list(session.scalars(
                select(HierarchyRecord.id).where(HierarchyRecord.source_id.in_(remove_source_ids))
            ))
            candidate_ids = self._select_ids_by_chunks(
                session,
                select(HierarchyMatchCandidate.id),
                HierarchyMatchCandidate.record_id,
                record_ids,
            )
            edge_ids = list(session.scalars(
                select(StructuralEdge.id).where(StructuralEdge.source_id.in_(remove_source_ids))
            ))
            removed_match_actions = self._delete_by_chunks(
                session, HierarchyMatchAction, HierarchyMatchAction.candidate_id, candidate_ids,
            )
            removed_candidates = self._delete_by_chunks(
                session, HierarchyMatchCandidate, HierarchyMatchCandidate.id, candidate_ids,
            )
            removed_graph_overrides = self._delete_by_chunks(
                session, StructuralEdgeAction, StructuralEdgeAction.edge_id, edge_ids,
            )
            removed_graph_edges = self._delete_by_chunks(
                session, StructuralEdge, StructuralEdge.id, edge_ids,
            )
            removed_records = self._delete_by_chunks(
                session, HierarchyRecord, HierarchyRecord.id, record_ids,
            )
            removed_sources = self._delete_by_chunks(
                session, HierarchySource, HierarchySource.id, remove_source_ids,
            )
            return HierarchyPruneResult(
                provider=provider_value,
                groups=len(groups),
                removed_sources=removed_sources,
                removed_records=removed_records,
                removed_candidates=removed_candidates,
                removed_match_actions=removed_match_actions,
                removed_graph_edges=removed_graph_edges,
                removed_graph_overrides=removed_graph_overrides,
            )

    @staticmethod
    def _delete_by_chunks(session: Session, model, column, ids: list[int]) -> int:
        removed = 0
        for chunk in _chunks(ids, 800):
            result = session.execute(delete(model).where(column.in_(chunk)))
            removed += int(result.rowcount or 0)
        return removed

    @staticmethod
    def _select_ids_by_chunks(session: Session, query, column, ids: list[int]) -> list[int]:
        values: list[int] = []
        for chunk in _chunks(ids, 800):
            values.extend(session.scalars(query.where(column.in_(chunk))))
        return values

    def summary(self, provider: str | None = None, *, source_id: int | None = None) -> dict[str, object]:
        provider_value = None if provider is None else provider.lower().strip()
        with self.session_factory() as session:
            source_query = (
                select(HierarchySource, func.count(HierarchyRecord.id))
                .join(HierarchyRecord, HierarchyRecord.source_id == HierarchySource.id, isouter=True)
                .group_by(HierarchySource.id)
                .order_by(HierarchySource.provider, HierarchySource.id)
            )
            candidate_status_query = (
                select(
                    HierarchyMatchCandidate.provider,
                    HierarchyMatchCandidate.status,
                    func.count(HierarchyMatchCandidate.id),
                )
                .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
                .group_by(HierarchyMatchCandidate.provider, HierarchyMatchCandidate.status)
                .order_by(HierarchyMatchCandidate.provider, HierarchyMatchCandidate.status)
            )
            candidate_method_query = (
                select(
                    HierarchyMatchCandidate.provider,
                    HierarchyMatchCandidate.match_method,
                    func.count(HierarchyMatchCandidate.id),
                )
                .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
                .group_by(HierarchyMatchCandidate.provider, HierarchyMatchCandidate.match_method)
                .order_by(HierarchyMatchCandidate.provider, HierarchyMatchCandidate.match_method)
            )
            record_count_query = (
                select(HierarchyRecord.provider, func.count(HierarchyRecord.id))
                .group_by(HierarchyRecord.provider)
                .order_by(HierarchyRecord.provider)
            )
            graph_relation_query = (
                select(
                    StructuralEdge.source,
                    StructuralEdge.relation_type,
                    func.count(StructuralEdge.id),
                )
                .where(StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES))
                .group_by(StructuralEdge.source, StructuralEdge.relation_type)
                .order_by(StructuralEdge.source, StructuralEdge.relation_type)
            )
            graph_status_query = (
                select(
                    StructuralEdge.source,
                    StructuralEdge.status,
                    func.count(StructuralEdge.id),
                )
                .where(StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES))
                .group_by(StructuralEdge.source, StructuralEdge.status)
                .order_by(StructuralEdge.source, StructuralEdge.status)
            )
            graph_geometry_query = (
                select(
                    StructuralEdge.source,
                    StructuralEdge.geometry_status,
                    func.count(StructuralEdge.id),
                )
                .where(StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES))
                .group_by(StructuralEdge.source, StructuralEdge.geometry_status)
                .order_by(StructuralEdge.source, StructuralEdge.geometry_status)
            )
            graph_role_query = (
                select(
                    StructuralEdge.source,
                    StructuralEdge.structural_role,
                    func.count(StructuralEdge.id),
                )
                .where(StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES))
                .group_by(StructuralEdge.source, StructuralEdge.structural_role)
                .order_by(StructuralEdge.source, StructuralEdge.structural_role)
            )
            matched_subquery = (
                select(
                    HierarchyMatchCandidate.provider.label("provider"),
                    HierarchyMatchCandidate.record_id.label("record_id"),
                    func.count(HierarchyMatchCandidate.id).label("candidate_count"),
                )
                .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
                .group_by(HierarchyMatchCandidate.provider, HierarchyMatchCandidate.record_id)
                .subquery()
            )
            matched_query = (
                select(
                    matched_subquery.c.provider,
                    func.count(matched_subquery.c.record_id),
                    func.sum(
                        (matched_subquery.c.candidate_count > 1).cast(Integer)
                    ),
                )
                .group_by(matched_subquery.c.provider)
                .order_by(matched_subquery.c.provider)
            )
            if provider_value is not None:
                source_query = source_query.where(HierarchySource.provider == provider_value)
                candidate_status_query = candidate_status_query.where(HierarchyMatchCandidate.provider == provider_value)
                candidate_method_query = candidate_method_query.where(HierarchyMatchCandidate.provider == provider_value)
                record_count_query = record_count_query.where(HierarchyRecord.provider == provider_value)
                graph_relation_query = graph_relation_query.where(StructuralEdge.source == provider_value)
                graph_status_query = graph_status_query.where(StructuralEdge.source == provider_value)
                graph_geometry_query = graph_geometry_query.where(StructuralEdge.source == provider_value)
                graph_role_query = graph_role_query.where(StructuralEdge.source == provider_value)
                matched_query = matched_query.where(matched_subquery.c.provider == provider_value)
            if source_id is not None:
                source_query = source_query.where(HierarchySource.id == source_id)
                candidate_status_query = candidate_status_query.where(HierarchyRecord.source_id == source_id)
                candidate_method_query = candidate_method_query.where(HierarchyRecord.source_id == source_id)
                record_count_query = record_count_query.where(HierarchyRecord.source_id == source_id)
                graph_relation_query = graph_relation_query.where(StructuralEdge.source_id == source_id)
                graph_status_query = graph_status_query.where(StructuralEdge.source_id == source_id)
                graph_geometry_query = graph_geometry_query.where(StructuralEdge.source_id == source_id)
                graph_role_query = graph_role_query.where(StructuralEdge.source_id == source_id)
                matched_subquery = (
                    select(
                        HierarchyMatchCandidate.provider.label("provider"),
                        HierarchyMatchCandidate.record_id.label("record_id"),
                        func.count(HierarchyMatchCandidate.id).label("candidate_count"),
                    )
                    .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
                    .where(HierarchyRecord.source_id == source_id)
                    .group_by(HierarchyMatchCandidate.provider, HierarchyMatchCandidate.record_id)
                    .subquery()
                )
                matched_query = (
                    select(
                        matched_subquery.c.provider,
                        func.count(matched_subquery.c.record_id),
                        func.sum((matched_subquery.c.candidate_count > 1).cast(Integer)),
                    )
                    .group_by(matched_subquery.c.provider)
                    .order_by(matched_subquery.c.provider)
                )
                if provider_value is not None:
                    matched_query = matched_query.where(matched_subquery.c.provider == provider_value)
            record_counts = {
                provider: count
                for provider, count in session.execute(record_count_query)
            }
            matched_counts = {
                provider: {
                    "matched_records": matched_records,
                    "ambiguous_records": ambiguous_records or 0,
                }
                for provider, matched_records, ambiguous_records in session.execute(matched_query)
            }
            return {
                "provider": provider_value,
                "source_id": source_id,
                "sources": [
                    {
                        "source_id": source.id,
                        "provider": source.provider,
                        "release": source.release,
                        "record_count": count,
                        "source_file": source.source_file,
                        "fetched_at": None if source.fetched_at is None else source.fetched_at.isoformat(),
                        "imported_at": source.imported_at.isoformat(),
                    }
                    for source, count in session.execute(source_query)
                ],
                "record_counts": [
                    {
                        "provider": provider,
                        "record_count": count,
                        "matched_records": matched_counts.get(provider, {}).get("matched_records", 0),
                        "unmatched_records": count - matched_counts.get(provider, {}).get("matched_records", 0),
                        "ambiguous_records": matched_counts.get(provider, {}).get("ambiguous_records", 0),
                    }
                    for provider, count in record_counts.items()
                ],
                "candidate_status_counts": [
                    {"provider": provider, "status": status, "count": count}
                    for provider, status, count in session.execute(candidate_status_query)
                ],
                "candidate_method_counts": [
                    {"provider": provider, "match_method": method, "count": count}
                    for provider, method, count in session.execute(candidate_method_query)
                ],
                "graph_relation_counts": [
                    {"provider": provider, "relation_type": relation_type, "count": count}
                    for provider, relation_type, count in session.execute(graph_relation_query)
                ],
                "graph_status_counts": [
                    {"provider": provider, "status": status, "count": count}
                    for provider, status, count in session.execute(graph_status_query)
                ],
                "graph_geometry_counts": [
                    {"provider": provider, "geometry_status": geometry_status, "count": count}
                    for provider, geometry_status, count in session.execute(graph_geometry_query)
                ],
                "graph_role_counts": [
                    {"provider": provider, "structural_role": structural_role, "count": count}
                    for provider, structural_role, count in session.execute(graph_role_query)
                ],
            }

    def match_records(
        self,
        provider: str,
        *,
        source_id: int | None = None,
        radius_arcsec: float = 30.0,
    ) -> HierarchyMatchResult:
        provider = provider.lower().strip()
        if provider not in {"wds", "ccdm"}:
            raise ValueError(f"unsupported hierarchy provider: {provider}")
        if radius_arcsec <= 0:
            raise ValueError("radius must be positive")
        with self.session_factory.begin() as session:
            query = select(HierarchyRecord).where(HierarchyRecord.provider == provider)
            if source_id is not None:
                query = query.where(HierarchyRecord.source_id == source_id)
            records = tuple(session.scalars(query.order_by(HierarchyRecord.id)))
            if records:
                record_ids = [record.id for record in records]
                session.execute(
                    delete(HierarchyMatchCandidate)
                    .where(HierarchyMatchCandidate.record_id.in_(record_ids))
                )
            alias_index = _build_alias_index(session)
            target_index = _build_target_index(tuple(session.scalars(select(Target))))
            components_by_system = _components_by_system(records)
            candidate_count = 0
            for record in records:
                candidates = _hierarchy_candidates_for_record(
                    record,
                    radius_arcsec,
                    alias_index,
                    target_index,
                    components_by_system=components_by_system,
                )
                for target_id, candidate in candidates.items():
                    session.add(HierarchyMatchCandidate(
                        record_id=record.id,
                        target_id=target_id,
                        provider=provider,
                        match_method="+".join(candidate["methods"]),
                        score=float(candidate["score"]),
                        separation_arcsec=candidate["separation_arcsec"],
                        identifier=candidate["identifier"],
                        reason="; ".join(candidate["reasons"]),
                    ))
                    candidate_count += 1
            return HierarchyMatchResult(
                provider=provider,
                source_id=source_id,
                record_count=len(records),
                candidate_count=candidate_count,
                radius_arcsec=radius_arcsec,
            )

    def derive_graph(
        self,
        provider: str,
        *,
        source_id: int | None = None,
    ) -> HierarchyGraphDeriveResult:
        provider = provider.lower().strip()
        if provider != "wds":
            raise ValueError(f"unsupported hierarchy graph provider: {provider}")
        with self.session_factory.begin() as session:
            query = select(HierarchyRecord).where(HierarchyRecord.provider == provider)
            if source_id is not None:
                query = query.where(HierarchyRecord.source_id == source_id)
            records = tuple(session.scalars(query.order_by(HierarchyRecord.source_id, HierarchyRecord.native_id, HierarchyRecord.id)))
            existing_edges = {
                _graph_edge_key(edge): edge
                for edge in session.scalars(
                    select(StructuralEdge).where(
                        StructuralEdge.source == provider,
                        StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES),
                        *([] if source_id is None else [StructuralEdge.source_id == source_id]),
                    )
                )
            }
            record_index = _build_wds_record_index(records)
            edge_count = 0
            skipped_count = 0
            refreshed_edge_ids: set[int] = set()
            derived_edges: list[StructuralEdge] = []
            for record in records:
                edge = _wds_graph_edge_for_record(record, record_index)
                if edge is None:
                    skipped_count += 1
                    continue
                derived_edges.append(edge)
                edge_count += 1
            _demote_ambiguous_structural_edges(derived_edges)
            for edge in derived_edges:
                existing = existing_edges.get(_graph_edge_key(edge))
                if existing is None:
                    session.add(edge)
                    session.flush()
                    refreshed_edge_ids.add(edge.id)
                else:
                    _copy_graph_edge_values(existing, edge)
                    refreshed_edge_ids.add(existing.id)
            existing_ids = {edge.id for edge in existing_edges.values()}
            stale_ids = sorted(existing_ids - refreshed_edge_ids)
            if stale_ids:
                referenced_stale_ids = set(session.scalars(
                    select(StructuralEdgeAction.edge_id)
                    .where(StructuralEdgeAction.edge_id.in_(stale_ids))
                ))
                for edge in existing_edges.values():
                    if edge.id in referenced_stale_ids:
                        edge.status = "stale"
                        edge.structural_role = "non_structural"
                        edge.note = _join_notes(edge.note, "stale graph edge retained because overrides reference it") or ""
                deletable_stale_ids = sorted(set(stale_ids) - referenced_stale_ids)
            else:
                deletable_stale_ids = []
            if deletable_stale_ids:
                session.execute(
                    delete(StructuralEdge)
                    .where(StructuralEdge.id.in_(deletable_stale_ids))
                )
            return HierarchyGraphDeriveResult(
                provider=provider,
                source_id=source_id,
                record_count=len(records),
                edge_count=edge_count,
                skipped_count=skipped_count,
            )

    def graph_edges(
        self,
        *,
        provider: str | None = None,
        native_id: str | None = None,
        target: str | int | None = None,
        source_id: int | None = None,
    ) -> tuple[HierarchyGraphEdgeRow, ...]:
        with self.session_factory() as session:
            query = select(StructuralEdge).where(StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES))
            if provider is not None:
                query = query.where(StructuralEdge.source == provider.lower().strip())
            if native_id is not None:
                query = query.where(StructuralEdge.native_id == native_id.strip())
            if source_id is not None:
                query = query.where(StructuralEdge.source_id == source_id)
            if target is not None:
                target_row = _find_required_target(session, target)
                record_ids = tuple(session.scalars(
                    select(HierarchyMatchCandidate.record_id)
                    .where(HierarchyMatchCandidate.target_id == target_row.id)
                    .where(HierarchyMatchCandidate.status.in_(["candidate", "accepted"]))
                ))
                if not record_ids:
                    return ()
                query = query.where(StructuralEdge.record_id.in_(record_ids))
            edges = tuple(session.scalars(query.order_by(
                StructuralEdge.source,
                StructuralEdge.native_id,
                StructuralEdge.reference_label,
                StructuralEdge.component_label,
                StructuralEdge.id,
            )))
            overrides = _latest_graph_overrides(session, list(edges))
            return tuple(_graph_edge_row(edge, overrides.get(edge.id)) for edge in edges)

    def graph_diagnostics(
        self,
        *,
        provider: str | None = None,
        source_id: int | None = None,
        native_id: str | None = None,
        limit: int = 100,
        severity: str | None = None,
        issue: str | None = None,
    ) -> tuple[HierarchyGraphDiagnosticRow, ...]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        severity_value = None if severity is None else severity.lower().strip()
        issue_value = None if issue is None else issue.strip()
        with self.session_factory() as session:
            query = select(StructuralEdge).where(StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES))
            provider_value = None if provider is None else provider.lower().strip()
            if provider_value is not None:
                query = query.where(StructuralEdge.source == provider_value)
            if source_id is not None:
                query = query.where(StructuralEdge.source_id == source_id)
            if native_id is not None:
                query = query.where(StructuralEdge.native_id == native_id.strip())
            edges = tuple(session.scalars(query.order_by(
                StructuralEdge.source,
                StructuralEdge.source_id,
                StructuralEdge.native_id,
                StructuralEdge.id,
            )))
            overrides = _latest_graph_overrides(session, list(edges))
            rows = [_graph_edge_row(edge, overrides.get(edge.id)) for edge in edges]

            candidate_query = (
                select(
                    HierarchyRecord.provider,
                    HierarchyRecord.source_id,
                    HierarchyRecord.native_id,
                    func.count(HierarchyMatchCandidate.id),
                )
                .join(HierarchyMatchCandidate, HierarchyMatchCandidate.record_id == HierarchyRecord.id)
                .group_by(HierarchyRecord.provider, HierarchyRecord.source_id, HierarchyRecord.native_id)
            )
            if provider_value is not None:
                candidate_query = candidate_query.where(HierarchyRecord.provider == provider_value)
            if source_id is not None:
                candidate_query = candidate_query.where(HierarchyRecord.source_id == source_id)
            if native_id is not None:
                candidate_query = candidate_query.where(
                    HierarchyRecord.native_id == native_id.strip()
                )
            candidate_counts = {
                (provider, source_id, native_id): count
                for provider, source_id, native_id, count in session.execute(candidate_query)
            }

        grouped: dict[tuple[str, int, str], list[HierarchyGraphEdgeRow]] = {}
        for row in rows:
            grouped.setdefault((row.provider, row.source_id, row.native_id), []).append(row)

        diagnostics: list[HierarchyGraphDiagnosticRow] = []
        for key, group_rows in grouped.items():
            row_provider, row_source_id, native_id = key
            matched_count = int(candidate_counts.get(key, 0))
            active_structural = [
                row for row in group_rows
                if row.structural_role == "structural" and row.status != "rejected"
            ]
            non_structural = [
                row for row in group_rows
                if row not in active_structural
            ]
            structural_count = len(active_structural)
            non_structural_count = len(non_structural)
            if matched_count and structural_count == 0:
                diagnostics.append(_graph_diagnostic_row(
                    row_provider,
                    row_source_id,
                    native_id,
                    "matched_without_structural_edges",
                    "review",
                    group_rows,
                    structural_count,
                    non_structural_count,
                    matched_count,
                    "matched hierarchy candidates exist, but no active structural graph edge remains",
                ))
            elif structural_count == 0 and non_structural_count > 0:
                diagnostics.append(_graph_diagnostic_row(
                    row_provider,
                    row_source_id,
                    native_id,
                    "only_non_structural_edges",
                    "info",
                    group_rows,
                    structural_count,
                    non_structural_count,
                    matched_count,
                    "graph has only non-structural/display edges",
                ))
            if active_structural:
                roots = _graph_structural_roots(active_structural)
                if len(roots) > 1:
                    diagnostics.append(_graph_diagnostic_row(
                        row_provider,
                        row_source_id,
                        native_id,
                        "disconnected_structural_groups",
                        "info",
                        group_rows,
                        structural_count,
                        non_structural_count,
                        matched_count,
                        f"structural roots: {', '.join(sorted(roots))}",
                    ))
                duplicate_parents = _graph_duplicate_parents(active_structural)
                if duplicate_parents:
                    diagnostics.append(_graph_diagnostic_row(
                        row_provider,
                        row_source_id,
                        native_id,
                        "duplicate_structural_parent",
                        "review",
                        group_rows,
                        structural_count,
                        non_structural_count,
                        matched_count,
                        "; ".join(
                            f"{component} from {', '.join(sorted(parents))}"
                            for component, parents in sorted(duplicate_parents.items())
                        ),
                    ))
                geometry_problems = [
                    row for row in active_structural
                    if matched_count and row.geometry_status != "usable"
                ]
                if geometry_problems:
                    diagnostics.append(_graph_diagnostic_row(
                        row_provider,
                        row_source_id,
                        native_id,
                        "structural_geometry_problem",
                        "review",
                        group_rows,
                        structural_count,
                        non_structural_count,
                        matched_count,
                        _graph_geometry_problem_detail(geometry_problems),
                    ))
        if severity_value is not None:
            diagnostics = [
                row for row in diagnostics
                if row.severity == severity_value
            ]
        if issue_value is not None:
            diagnostics = [
                row for row in diagnostics
                if row.issue == issue_value
            ]
        diagnostics.sort(key=lambda row: (
            0 if row.severity == "review" else 1,
            row.provider,
            row.source_id,
            row.native_id,
            row.issue,
        ))
        if limit == 0:
            return tuple(diagnostics)
        return tuple(diagnostics[:limit])

    def override_graph_edge(
        self,
        *,
        provider: str,
        native_id: str,
        reference_label: str,
        component_label: str,
        actor: str | None,
        reason: str | None = None,
        source_id: int | None = None,
        status: str | None = None,
        relation_type: str | None = None,
        structural_role: str | None = None,
    ) -> HierarchyGraphOverrideResult:
        clean_provider = provider.lower().strip()
        clean_native = native_id.strip()
        clean_reference = reference_label.strip()
        clean_component = component_label.strip()
        clean_status = status.strip() if status is not None else None
        clean_relation_type = relation_type.strip() if relation_type is not None else None
        clean_structural_role = structural_role.strip() if structural_role is not None else None
        if clean_structural_role is not None and clean_structural_role not in {"structural", "non_structural"}:
            raise ValueError("structural role must be structural or non_structural")
        if clean_status is None and clean_relation_type is None and clean_structural_role is None:
            raise ValueError("status, relation type, or structural role override is required")
        with self.session_factory.begin() as session:
            query = select(StructuralEdge).where(
                StructuralEdge.source == clean_provider,
                StructuralEdge.native_id == clean_native,
                StructuralEdge.reference_label == clean_reference,
                StructuralEdge.component_label == clean_component,
                StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES),
            )
            if source_id is not None:
                query = query.where(StructuralEdge.source_id == source_id)
            matches = tuple(session.scalars(query.order_by(StructuralEdge.id)))
            if not matches:
                raise KeyError(f"hierarchy graph edge not found: {clean_provider} {clean_native} {clean_reference}->{clean_component}")
            if len(matches) > 1 and source_id is None:
                raise ValueError("multiple graph edges matched; supply --source-id")
            edge = matches[0]
            requested = ", ".join(
                f"{name}={value}"
                for name, value in (
                    ("status", clean_status),
                    ("type", clean_relation_type),
                    ("role", clean_structural_role),
                )
                if value is not None
            )
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Overrode {edge.source} structural edge {edge.native_id} "
                    f"{edge.reference_label}->{edge.component_label}: {requested}"
                ),
            )
            latest = _latest_graph_overrides(session, [edge]).get(edge.id)
            previous_status = latest.new_status if latest is not None and latest.new_status is not None else edge.status
            previous_relation_type = (
                latest.new_relation_type
                if latest is not None and latest.new_relation_type is not None
                else edge.relation_type
            )
            previous_structural_role = (
                latest.new_structural_role
                if latest is not None and latest.new_structural_role is not None
                else edge.structural_role
            )
            override = StructuralEdgeAction(
                edge_id=edge.id,
                source=edge.source,
                native_id=edge.native_id,
                reference_label=edge.reference_label,
                component_label=edge.component_label,
                action="override_edge",
                previous_status=previous_status,
                new_status=clean_status,
                previous_relation_type=previous_relation_type,
                new_relation_type=clean_relation_type,
                previous_structural_role=previous_structural_role,
                new_structural_role=clean_structural_role,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(override)
            session.flush()
            return HierarchyGraphOverrideResult(
                override_id=override.id,
                edge_id=edge.id,
                previous_status=previous_status,
                new_status=clean_status or previous_status,
                previous_relation_type=previous_relation_type,
                new_relation_type=clean_relation_type or previous_relation_type,
                previous_structural_role=previous_structural_role,
                new_structural_role=clean_structural_role or previous_structural_role,
                actor=decision.actor,
                reason=decision.reason,
            )

    def review_matches(self, provider: str | None = None) -> tuple[HierarchyMatchReviewRow, ...]:
        with self.session_factory() as session:
            query = (
                select(HierarchyMatchCandidate, HierarchyRecord, Target)
                .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
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
                query = query.where(HierarchyMatchCandidate.provider == provider.lower().strip())
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
            candidate, record, target = _find_required_candidate_context(session, candidate_id)
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
                status=_RELATIONSHIP_STATUS,
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
            candidate, record, target = _find_required_candidate_context(session, candidate_id)
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

def _target_context_system_keys(
    context: dict[str, object],
) -> set[tuple[str, int, str]]:
    keys = set()
    for system in context.get("systems") or []:
        source_id = system.get("source_id")
        if source_id is None:
            continue
        keys.add((
            str(system["provider"]),
            int(source_id),
            str(system["native_id"]),
        ))
    return keys


def _system_component_positions(context: dict[str, object]) -> list[dict[str, object]]:
    rows: dict[tuple[str, int, str, str], dict[str, object]] = {}
    for system in context.get("systems") or []:
        source_id = system.get("source_id")
        if source_id is None:
            continue
        provider = str(system["provider"])
        native_id = str(system["native_id"])
        for component in system.get("components") or []:
            label = str(component["component"])
            key = (provider, int(source_id), native_id, label)
            value = {
                "provider": provider,
                "source_id": int(source_id),
                "native_id": native_id,
                "component": label,
                "ra_deg": component["ra_deg"],
                "dec_deg": component["dec_deg"],
                "separation_from_target_arcsec": component["separation_arcsec"],
                "role": component["role"],
                "edge_id": component["edge_id"],
                "relation_type": component["relation_type"],
                "structural_role": component["structural_role"],
                "geometry_status": component["geometry_status"],
            }
            existing = rows.get(key)
            if (
                existing is None
                or float(value["separation_from_target_arcsec"])
                < float(existing["separation_from_target_arcsec"])
            ):
                rows[key] = value
    return sorted(
        rows.values(),
        key=lambda row: (
            float(row["separation_from_target_arcsec"]),
            str(row["provider"]),
            str(row["native_id"]),
            str(row["component"]),
        ),
    )


def _system_context_radius_arcsec(
    component_positions: list[dict[str, object]],
) -> float:
    if not component_positions:
        return 60.0
    maximum = max(float(row["separation_from_target_arcsec"]) for row in component_positions)
    return min(max(60.0, maximum + 15.0), 300.0)


def _nearby_sdb_targets(
    session: Session,
    target: Target,
    *,
    radius_arcsec: float,
) -> list[dict[str, object]]:
    origin = Astrometry(target.ra2000_deg, target.dec2000_deg)
    rows = []
    for other in session.scalars(select(Target).order_by(Target.sdbid)):
        separation = angular_separation_arcsec(
            origin,
            Astrometry(other.ra2000_deg, other.dec2000_deg),
        )
        if separation > radius_arcsec:
            continue
        rows.append(_system_target_review_row(session, target, other))
    return sorted(rows, key=lambda row: float(row["separation_arcsec"]))


def _explicit_system_target_ids(session: Session, target_id: int) -> set[int]:
    system_ids = set(session.scalars(select(TargetSystemMember.system_id).where(
        TargetSystemMember.target_id == target_id
    )))
    if not system_ids:
        return {target_id}
    return set(session.scalars(select(TargetSystemMember.target_id).where(
        TargetSystemMember.system_id.in_(system_ids)
    ))) | {target_id}


def _system_target_review_row(
    session: Session,
    requested_target: Target,
    target: Target,
) -> dict[str, object]:
    separation = angular_separation_arcsec(
        Astrometry(requested_target.ra2000_deg, requested_target.dec2000_deg),
        Astrometry(target.ra2000_deg, target.dec2000_deg),
    )
    identifiers = list(session.scalars(
        select(ExternalIdentifier.value)
        .where(ExternalIdentifier.target_id == target.id)
        .order_by(ExternalIdentifier.source, ExternalIdentifier.value)
        .limit(12)
    ))
    canonical = (
        None if target.canonical_astrometry_id is None
        else session.get(AstrometricSolution, target.canonical_astrometry_id)
    )
    return {
        "target_id": target.id,
        "sdbid": target.sdbid,
        "ra2000_deg": target.ra2000_deg,
        "dec2000_deg": target.dec2000_deg,
        "separation_arcsec": separation,
        "is_requested_target": target.id == requested_target.id,
        "canonical_astrometry": None if canonical is None else {
            "source": canonical.source,
            "source_id": canonical.source_id,
            "pm_ra_cosdec_masyr": canonical.pm_ra_cosdec_masyr,
            "pm_dec_masyr": canonical.pm_dec_masyr,
            "proper_motion_available": canonical.proper_motion_available,
        },
        "identifiers": identifiers,
    }


def _system_simbad_metadata(
    session: Session,
    target_ids: list[int],
) -> dict[str, dict[str, object]]:
    if not target_ids:
        return {}
    targets = {
        target.id: target.sdbid
        for target in session.scalars(
            select(Target).where(Target.id.in_(target_ids))
        )
    }
    result: dict[str, dict[str, object]] = {}
    rows = session.execute(
        select(MetadataRun, SimbadMetadata)
        .join(SimbadMetadata, SimbadMetadata.run_id == MetadataRun.id)
        .where(
            MetadataRun.target_id.in_(target_ids),
            MetadataRun.provider == "simbad",
            MetadataRun.is_current.is_(True),
            MetadataRun.status == "match",
        )
        .order_by(MetadataRun.target_id, MetadataRun.id.desc())
    )
    for run, metadata in rows:
        sdbid = targets.get(run.target_id)
        if sdbid is None or sdbid in result:
            continue
        parallax = metadata.parallax_mas
        parallax_error = metadata.parallax_error_mas
        distance_pc = (
            None
            if parallax is None or parallax <= 0
            else 1000.0 / parallax
        )
        distance_error_pc = (
            None
            if distance_pc is None
            or parallax_error is None
            or parallax_error < 0
            else 1000.0 * parallax_error / (parallax * parallax)
        )
        result[sdbid] = {
            "run_id": run.id,
            "main_id": metadata.main_id,
            "spectral_type": metadata.spectral_type,
            "primary_object_type": metadata.primary_object_type,
            "parallax_mas": parallax,
            "parallax_error_mas": parallax_error,
            "distance_pc": distance_pc,
            "distance_error_pc": distance_error_pc,
        }
    return result


def _system_simbad_main_ids(
    session: Session,
    target_ids: list[int],
    *,
    metadata_by_target: dict[str, dict[str, object]],
) -> dict[str, str]:
    """Return stable SIMBAD display IDs, including imported relatives.

    Identity imports store SIMBAD's main ID first among their SIMBAD-sourced
    identifiers even before a metadata refresh creates a current
    ``SimbadMetadata`` row. Current metadata remains authoritative when both
    forms are available.
    """
    result = {
        sdbid: str(metadata["main_id"])
        for sdbid, metadata in metadata_by_target.items()
        if metadata.get("main_id")
    }
    if not target_ids:
        return result
    targets = {
        target.id: target.sdbid
        for target in session.scalars(
            select(Target).where(Target.id.in_(target_ids))
        )
    }
    for source in ("simbad_main_id", "simbad"):
        rows = session.execute(
            select(ExternalIdentifier.target_id, ExternalIdentifier.value)
            .where(
                ExternalIdentifier.target_id.in_(target_ids),
                ExternalIdentifier.source == source,
            )
            .order_by(ExternalIdentifier.target_id, ExternalIdentifier.id)
        )
        for target_id, value in rows:
            sdbid = targets.get(target_id)
            if sdbid is not None:
                result.setdefault(sdbid, value)
    return result


def _system_hierarchy_candidates(
    session: Session,
    target_ids: list[int],
    *,
    system_keys: set[tuple[str, int, str]],
) -> dict[str, list[dict[str, object]]]:
    if not target_ids or not system_keys:
        return {}
    targets = {
        target.id: target.sdbid
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
    }
    result: dict[str, list[dict[str, object]]] = {sdbid: [] for sdbid in targets.values()}
    rows = session.execute(
        select(HierarchyMatchCandidate, HierarchyRecord)
        .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
        .where(HierarchyMatchCandidate.target_id.in_(target_ids))
        .order_by(
            HierarchyMatchCandidate.target_id,
            HierarchyRecord.provider,
            HierarchyRecord.native_id,
            HierarchyMatchCandidate.score.desc(),
            HierarchyMatchCandidate.id,
        )
    )
    for candidate, record in rows:
        key = (record.provider, record.source_id, record.native_id)
        if key not in system_keys:
            continue
        result[targets[candidate.target_id]].append(
            _target_context_candidate(candidate, record)
        )
    return {key: value for key, value in result.items() if value}


def _annotate_system_component_targets(
    component_positions: list[dict[str, object]],
    *,
    nearby_targets: list[dict[str, object]],
    hierarchy_candidates_by_target: dict[str, list[dict[str, object]]],
    requested_sdbid: str,
    position_threshold_arcsec: float = 2.0,
) -> list[dict[str, object]]:
    identifier_matches: dict[tuple[str, int, str, str], list[dict[str, object]]] = {}
    target_by_sdbid = {str(row["sdbid"]): row for row in nearby_targets}
    for sdbid, candidates in hierarchy_candidates_by_target.items():
        target = target_by_sdbid.get(sdbid)
        if target is None:
            continue
        for candidate in candidates:
            if candidate.get("status") == "rejected":
                continue
            match_method = str(candidate.get("match_method") or "")
            if "identifier" not in match_method:
                continue
            source_id = candidate.get("record_source_id")
            if source_id is None:
                continue
            for component in _component_labels_for_identifier_candidate(candidate):
                key = (
                    str(candidate["provider"]),
                    int(source_id),
                    str(candidate["native_id"]),
                    str(component),
                )
                identifier_matches.setdefault(key, []).append({
                    "target_id": target["target_id"],
                    "sdbid": sdbid,
                    "candidate_id": candidate["candidate_id"],
                    "match_method": match_method,
                    "separation_arcsec": candidate.get("separation_arcsec"),
                })

    annotated = []
    for component in component_positions:
        key = (
            str(component["provider"]),
            int(component["source_id"]),
            str(component["native_id"]),
            str(component["component"]),
        )
        id_matches = sorted(
            identifier_matches.get(key, []),
            key=lambda row: (
                row["separation_arcsec"] is None,
                row["separation_arcsec"] if row["separation_arcsec"] is not None else math.inf,
                row["sdbid"],
            ),
        )
        sky_match = _nearest_component_sky_target(
            component,
            nearby_targets,
            threshold_arcsec=position_threshold_arcsec,
        )
        id_sdbids = tuple(dict.fromkeys(str(row["sdbid"]) for row in id_matches))
        chosen_sdbid = None
        chosen_target_id = None
        match_basis = "none"
        match_separation = None if sky_match is None else sky_match["component_match_separation_arcsec"]
        conflict = None
        if len(id_sdbids) > 1:
            conflict = "multiple_identifier_target_matches"
            chosen_sdbid = id_sdbids[0]
            chosen_target_id = target_by_sdbid[chosen_sdbid]["target_id"]
            match_basis = "identifier_conflict"
        elif id_sdbids:
            chosen_sdbid = id_sdbids[0]
            chosen_target_id = target_by_sdbid[chosen_sdbid]["target_id"]
            if sky_match is None:
                match_basis = "identifier"
            elif sky_match["sdbid"] == chosen_sdbid:
                match_basis = "identifier+position"
            else:
                match_basis = "identifier_position_conflict"
                conflict = "identifier_and_position_target_disagree"
        elif sky_match is not None:
            chosen_sdbid = sky_match["sdbid"]
            chosen_target_id = sky_match["target_id"]
            match_basis = "position"

        if conflict is not None:
            role = "conflicted_component_assignment"
        elif chosen_sdbid is None:
            role = "known_unimported_component"
        elif chosen_sdbid == requested_sdbid:
            role = "current_target"
        else:
            role = "sibling_target"

        annotated.append({
            **component,
            "linked_sdbid": chosen_sdbid,
            "linked_target_id": chosen_target_id,
            "component_target_role": role,
            "component_match_basis": match_basis,
            "component_match_separation_arcsec": match_separation,
            "component_match_conflict": conflict,
            "identifier_match_sdbids": list(id_sdbids),
            "sky_match_sdbid": None if sky_match is None else sky_match["sdbid"],
            "sky_match_separation_arcsec": None if sky_match is None else sky_match["component_match_separation_arcsec"],
            "position_match_threshold_arcsec": position_threshold_arcsec,
        })
    return annotated


def _component_labels_for_identifier_candidate(
    candidate: dict[str, object],
) -> tuple[str, ...]:
    values = []
    identifier = candidate.get("identifier")
    if identifier:
        label = _component_label_from_identifier(str(identifier))
        if label:
            values.append(label)
    component = candidate.get("component")
    if component:
        values.append(str(component))
    return tuple(dict.fromkeys(values))


def _nearest_component_sky_target(
    component: dict[str, object],
    nearby_targets: list[dict[str, object]],
    *,
    threshold_arcsec: float,
) -> dict[str, object] | None:
    ra = component.get("ra_deg")
    dec = component.get("dec_deg")
    if ra is None or dec is None:
        return None
    origin = Astrometry(float(ra), float(dec))
    candidates = []
    for target in nearby_targets:
        separation = angular_separation_arcsec(
            origin,
            Astrometry(float(target["ra2000_deg"]), float(target["dec2000_deg"])),
        )
        if separation <= threshold_arcsec:
            candidates.append({
                **target,
                "component_match_separation_arcsec": separation,
            })
    return min(
        candidates,
        key=lambda row: (float(row["component_match_separation_arcsec"]), str(row["sdbid"])),
        default=None,
    )


def _system_photometry(
    session: Session,
    target_ids: list[int],
) -> dict[str, list[dict[str, object]]]:
    if not target_ids:
        return {}
    targets = {
        target.id: target.sdbid
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
    }
    result: dict[str, list[dict[str, object]]] = {sdbid: [] for sdbid in targets.values()}
    for encounter in current_measurement_encounters(session, target_ids):
        measurement = encounter.measurement
        result[targets[encounter.target_id]].append({
            "measurement_id": measurement.id,
            "provider": measurement.provider,
            "source_id": measurement.source_id,
            "band": measurement.band,
            "value": measurement.value,
            "error": measurement.error,
            "unit": measurement.unit,
            "resolution_major_arcsec": measurement.resolution_major_arcsec,
            "resolution_minor_arcsec": measurement.resolution_minor_arcsec,
            "ownership_scope": measurement.ownership_scope,
            "blend_state": measurement.blend_state,
            "excluded": measurement.excluded,
        })
    return {key: value for key, value in result.items() if value}


def _system_target_lifecycle(
    session: Session,
    target_ids: list[int],
) -> dict[str, dict[str, object]]:
    targets = {
        target.id: target
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
    }
    actions: dict[int, TargetLifecycleAction] = {}
    for action in session.scalars(
        select(TargetLifecycleAction)
        .where(TargetLifecycleAction.target_id.in_(target_ids))
        .order_by(TargetLifecycleAction.id)
    ):
        actions[action.target_id] = action
    result = {}
    for target_id, target in targets.items():
        action = actions.get(target_id)
        replacement = (
            None if action is None or action.superseded_by_target_id is None
            else session.get(Target, action.superseded_by_target_id)
        )
        result[target.sdbid] = {
            "target_id": target.id,
            "role": "unspecified" if action is None else action.role,
            "state": "active" if action is None else action.state,
            "superseded_by_sdbid": None if replacement is None else replacement.sdbid,
            "action_id": None if action is None else action.id,
        }
    return dict(sorted(result.items()))


def _system_memberships(
    session: Session,
    target_ids: list[int],
) -> dict[str, list[dict[str, object]]]:
    if not target_ids:
        return {}
    targets = {
        target.id: target.sdbid
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
    }
    result: dict[str, list[dict[str, object]]] = {}
    rows = session.execute(
        select(TargetSystemMember, TargetSystem)
        .join(TargetSystem, TargetSystem.id == TargetSystemMember.system_id)
        .where(TargetSystemMember.target_id.in_(target_ids))
        .order_by(TargetSystem.name, TargetSystemMember.component_label, TargetSystemMember.id)
    )
    for member, system in rows:
        sdbid = targets.get(member.target_id)
        if sdbid is None:
            continue
        result.setdefault(sdbid, []).append({
            "system_id": system.id,
            "system_name": system.name,
            "component_label": member.component_label,
            "source": member.source,
            "is_primary": system.primary_target_id == member.target_id,
        })
    return dict(sorted(result.items()))


def _system_measurement_assignments(
    session: Session,
    target_ids: list[int],
) -> list[dict[str, object]]:
    if not target_ids:
        return []
    encounters = current_measurement_encounters(session, target_ids)
    measurements = list({
        encounter.measurement.id: encounter.measurement for encounter in encounters
    }.values())
    measurements.sort(key=lambda value: (
        value.provider, value.source_id, value.band, value.id,
    ))
    if not measurements:
        return []
    measurement_ids = [value.id for value in measurements]
    associations_by_measurement: dict[int, list[MeasurementTargetAssociation]] = {}
    for association in session.scalars(
        select(MeasurementTargetAssociation)
        .where(MeasurementTargetAssociation.measurement_id.in_(measurement_ids))
        .order_by(MeasurementTargetAssociation.measurement_id, MeasurementTargetAssociation.id)
    ):
        associations_by_measurement.setdefault(association.measurement_id, []).append(association)
    referenced_target_ids = {
        value.target_id for values in associations_by_measurement.values() for value in values
    } | {value.target_id for value in measurements}
    targets = {
        target.id: target.sdbid
        for target in session.scalars(select(Target).where(Target.id.in_(referenced_target_ids)))
    }
    return [{
        "measurement_id": measurement.id,
        "origin_target_id": measurement.target_id,
        "origin_sdbid": targets.get(measurement.target_id),
        "provider": measurement.provider,
        "source_id": measurement.source_id,
        "band": measurement.band,
        "value": measurement.value,
        "unit": measurement.unit,
        "contributors": [{
            "association_id": association.id,
            "target_id": association.target_id,
            "sdbid": targets.get(association.target_id),
            "role": association.role,
            "method": association.method,
            "weight": association.weight,
            "note": association.note,
        } for association in associations_by_measurement.get(measurement.id, [])],
    } for measurement in measurements]


def _system_catalog_neighbourhood(
    session: Session,
    target_ids: list[int],
) -> dict[str, list[dict[str, object]]]:
    if not target_ids:
        return {}
    targets = {
        target.id: target.sdbid
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
    }
    result: dict[str, list[dict[str, object]]] = {sdbid: [] for sdbid in targets.values()}
    rows = session.execute(
        select(CatalogRun, RawCatalogRow)
        .join(RawCatalogRow, RawCatalogRow.run_id == CatalogRun.id)
        .where(
            CatalogRun.target_id.in_(target_ids),
            CatalogRun.is_current.is_(True),
        )
        .order_by(
            CatalogRun.target_id,
            CatalogRun.provider,
            RawCatalogRow.accepted.desc(),
            RawCatalogRow.score.desc(),
            RawCatalogRow.separation_arcsec,
            RawCatalogRow.id,
        )
    )
    for run, row in rows:
        payload = _json_payload(row.payload_json)
        result[targets[run.target_id]].append({
            "provider": run.provider,
            "run_id": run.id,
            "raw_row_id": row.id,
            "source_id": row.source_id,
            "accepted": row.accepted,
            "run_status": run.status,
            "separation_arcsec": row.separation_arcsec,
            "score": row.score,
            "ra_deg": row.ra_deg,
            "dec_deg": row.dec_deg,
            "epoch": row.epoch,
            "neighbourhood_flags": _catalog_neighbourhood_flags(run.provider, payload),
        })
    return {key: value for key, value in result.items() if value}


def _catalog_neighbourhood_flags(
    provider: str, payload: dict[str, object]
) -> dict[str, object]:
    payload = normalize_review_payload(provider, payload)
    review = payload.get("_sdb_review")
    flags = review.get("neighbourhood_flags") if isinstance(review, dict) else None
    return dict(flags) if isinstance(flags, dict) else {}


def _json_payload(payload_json: str | None) -> dict[str, object]:
    try:
        value = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _identity_cross_candidates(
    session: Session,
    target: Target,
    *,
    nearby_target_ids: set[int],
) -> list[dict[str, object]]:
    if not nearby_target_ids:
        return []
    nearby_target_ids = set(nearby_target_ids) - {target.id}
    if not nearby_target_ids:
        return []
    source_index = _target_source_index(session, nearby_target_ids)
    rows = []
    for candidate in session.scalars(
        select(MatchCandidate)
        .join(Submission, Submission.id == MatchCandidate.submission_id)
        .where(Submission.target_id == target.id)
        .order_by(MatchCandidate.provider, MatchCandidate.separation_arcsec)
    ):
        keys = {
            normalize_identifier(candidate.source_id),
            normalize_identifier(f"{candidate.provider} {candidate.source_id}"),
        }
        if candidate.provider == "gaia_dr3":
            keys.add(normalize_identifier(f"Gaia DR3 {candidate.source_id}"))
        matched_targets = []
        seen_target_ids = set()
        for key in keys:
            for other in source_index.get(key, []):
                if other["target_id"] in seen_target_ids:
                    continue
                seen_target_ids.add(other["target_id"])
                matched_targets.append(other)
        if not matched_targets:
            continue
        rows.append({
            "candidate_id": candidate.id,
            "provider": candidate.provider,
            "source_id": candidate.source_id,
            "accepted": candidate.accepted,
            "separation_arcsec": candidate.separation_arcsec,
            "score": candidate.score,
            "matched_nearby_targets": matched_targets,
        })
    return rows


def _target_source_index(
    session: Session,
    target_ids: set[int],
) -> dict[str, list[dict[str, object]]]:
    targets = {
        target.id: target.sdbid
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
    }
    index: dict[str, list[dict[str, object]]] = {}
    for identifier in session.scalars(
        select(ExternalIdentifier)
        .where(ExternalIdentifier.target_id.in_(target_ids))
        .order_by(ExternalIdentifier.target_id, ExternalIdentifier.id)
    ):
        index.setdefault(identifier.normalized_value, []).append({
            "target_id": identifier.target_id,
            "sdbid": targets[identifier.target_id],
            "match_source": "external_identifier",
            "identifier": identifier.value,
        })
    for solution in session.scalars(
        select(AstrometricSolution)
        .where(AstrometricSolution.target_id.in_(target_ids))
        .where(AstrometricSolution.source_id.is_not(None))
        .order_by(AstrometricSolution.target_id, AstrometricSolution.id)
    ):
        values = {
            str(solution.source_id),
            f"{solution.source} {solution.source_id}",
        }
        if solution.source == "gaia_dr3":
            values.add(f"Gaia DR3 {solution.source_id}")
        for value in values:
            index.setdefault(normalize_identifier(value), []).append({
                "target_id": solution.target_id,
                "sdbid": targets[solution.target_id],
                "match_source": "astrometric_solution",
                "identifier": value,
            })
    return index


def _target_context_candidate(candidate: HierarchyMatchCandidate, record: HierarchyRecord) -> dict[str, object]:
    return {
        "candidate_id": candidate.id,
        "record_id": record.id,
        "record_source_id": record.source_id,
        "provider": candidate.provider,
        "status": candidate.status,
        "native_id": record.native_id,
        "component": record.component,
        "discoverer_id": record.discoverer_id,
        "match_method": candidate.match_method,
        "score": candidate.score,
        "separation_arcsec": candidate.separation_arcsec,
        "identifier": candidate.identifier,
        "reason": candidate.reason,
        "record_ra_deg": record.ra_deg,
        "record_dec_deg": record.dec_deg,
        "record_separation_arcsec": record.separation_arcsec,
        "record_pa_deg": record.pa_deg,
        "record_epoch": record.measure_epoch,
        "magnitude_primary": record.magnitude_primary,
        "magnitude_secondary": record.magnitude_secondary,
        "delta_mag": record.delta_mag,
    }


def _target_context_components(
    target_position: Astrometry,
    edges: list[HierarchyGraphEdgeRow],
) -> list[dict[str, object]]:
    components: dict[str, dict[str, object]] = {}
    for edge in edges:
        if edge.status == "rejected":
            continue
        if edge.reference_label and edge.start_ra_deg is not None and edge.start_dec_deg is not None:
            _target_context_add_component(
                components,
                target_position,
                edge.reference_label,
                edge.start_ra_deg,
                edge.start_dec_deg,
                edge,
                "reference",
            )
        if edge.component_label and edge.end_ra_deg is not None and edge.end_dec_deg is not None:
            _target_context_add_component(
                components,
                target_position,
                edge.component_label,
                edge.end_ra_deg,
                edge.end_dec_deg,
                edge,
                "component",
            )
    return sorted(
        components.values(),
        key=lambda item: (
            item["separation_arcsec"],
            str(item["component"]),
        ),
    )


def _target_context_add_component(
    components: dict[str, dict[str, object]],
    target_position: Astrometry,
    component: str,
    ra_deg: float,
    dec_deg: float,
    edge: HierarchyGraphEdgeRow,
    role: str,
) -> None:
    separation = angular_separation_arcsec(target_position, Astrometry(ra_deg, dec_deg))
    existing = components.get(component)
    if existing is not None and existing["separation_arcsec"] <= separation:
        return
    components[component] = {
        "component": component,
        "ra_deg": ra_deg,
        "dec_deg": dec_deg,
        "separation_arcsec": separation,
        "edge_id": edge.edge_id,
        "role": role,
        "relation_type": edge.relation_type,
        "structural_role": edge.structural_role,
        "geometry_status": edge.geometry_status,
    }


def _position_separation_arcsec(
    first_ra_deg: float,
    first_dec_deg: float,
    second_ra_deg: float,
    second_dec_deg: float,
) -> float:
    return angular_separation_arcsec(
        Astrometry(first_ra_deg, first_dec_deg),
        Astrometry(second_ra_deg, second_dec_deg),
    )


def _target_context_classification(
    *,
    systems: list[dict[str, object]],
    nearest_component: dict[str, object] | None,
    closest_companion: dict[str, object] | None,
    review_required: bool,
) -> str:
    if review_required:
        return "review_required"
    if not systems:
        return "single_or_no_known_hierarchy"
    if nearest_component is None:
        return "known_hierarchy_without_component_geometry"
    if closest_companion is not None:
        return "component_of_known_system"
    return "component_of_known_system"


def _target_component_assignment(
    *,
    semantic_identity: dict[str, object],
    nearest_component: dict[str, object] | None,
    closest_companion: dict[str, object] | None,
    systems: list[dict[str, object]],
    review_required: bool,
) -> dict[str, object]:
    semantic_kind = str(semantic_identity["kind"])
    semantic_component = _best_component_label_candidate(semantic_identity.get("component_label_candidates") or [])
    relevance_counts = semantic_identity.get("relationship_relevance_counts") or {}
    stellar_relationships = int(relevance_counts.get("stellar_or_substellar_component", 0))
    nonstellar_relationships = (
        int(relevance_counts.get("planetary_or_disk", 0))
        + int(relevance_counts.get("contextual_group", 0))
    )
    nearest_label = None if nearest_component is None else nearest_component["component"]
    nearest_sep = None if nearest_component is None else nearest_component["separation_arcsec"]
    component_counts = [len(system["components"]) for system in systems]
    disconnected_groups = [
        system for system in systems
        for diagnostic in system["diagnostics"]
        if diagnostic["issue"] == "disconnected_structural_groups"
    ]
    if review_required:
        status = "review_required"
        confidence = "low"
        reason = "review-level hierarchy diagnostics are present"
    elif nearest_component is None:
        status = "semantic_only" if semantic_kind != "unknown" else "no_assignment"
        confidence = "low" if semantic_kind == "unknown" else "medium"
        reason = "no provider component geometry is available"
    elif semantic_component and nearest_label and _component_labels_match(semantic_component, str(nearest_label)):
        status = "semantic_geometry_agree"
        confidence = "high"
        reason = "SIMBAD component label candidate agrees with nearest provider component"
    elif semantic_component and nearest_label and _component_label_contains(semantic_component, str(nearest_label)):
        status = "semantic_group_contains_nearest_component"
        confidence = "medium"
        reason = "SIMBAD component label candidate is a group containing the nearest provider component"
    elif semantic_component and nearest_label:
        status = "semantic_geometry_conflict"
        confidence = "low"
        reason = "SIMBAD component label candidate conflicts with nearest provider component"
    elif disconnected_groups and semantic_kind in {"system_or_parent", "subsystem"}:
        status = "ambiguous_disconnected_groups"
        confidence = "low"
        reason = "provider geometry has disconnected top-level groups; do not assume one parent system without semantic support"
    elif semantic_kind == "unknown":
        status = "geometry_only"
        confidence = "medium"
        reason = "nearest provider component is based on geometry only"
    elif stellar_relationships == 0 and nonstellar_relationships > 0:
        status = "semantic_hierarchy_not_stellar_component"
        confidence = "medium"
        reason = "SIMBAD hierarchy relationships are non-stellar/contextual for component-blending purposes"
    elif semantic_kind == "single_or_no_known_hierarchy":
        status = "geometry_has_hierarchy_but_simbad_does_not"
        confidence = "medium"
        reason = "SIMBAD has no hierarchy relationships, but provider geometry has nearby components"
    elif semantic_kind == "system_or_parent":
        status = "system_level_target"
        confidence = "high"
        reason = "SIMBAD marks this target as a parent/system; provider components are contextual geometry"
    elif semantic_kind in {"component", "subsystem"}:
        status = "semantic_component_label_unknown"
        confidence = "medium"
        reason = "SIMBAD hierarchy says this is a component/subsystem, but no component label has been parsed yet"
    else:
        status = "unclassified"
        confidence = "low"
        reason = f"unhandled semantic identity kind: {semantic_kind}"
    return {
        "status": status,
        "confidence": confidence,
        "reason": reason,
        "semantic_kind": semantic_kind,
        "semantic_main_id": semantic_identity["main_id"],
        "semantic_component": semantic_component,
        "nearest_component": nearest_label,
        "nearest_separation_arcsec": nearest_sep,
        "closest_companion_component": None if closest_companion is None else closest_companion["component"],
        "closest_companion_separation_arcsec": None if closest_companion is None else closest_companion.get("separation_from_nearest_arcsec"),
        "matched_systems": len(systems),
        "component_counts": component_counts,
        "relationship_relevance_counts": relevance_counts,
        "evidence": _target_assignment_evidence(semantic_kind, nearest_component),
        "review_required": review_required,
    }


def _target_assignment_evidence(
    semantic_kind: str,
    nearest_component: dict[str, object] | None,
) -> list[str]:
    evidence = []
    if semantic_kind != "unknown":
        evidence.append("simbad_relationships")
    if nearest_component is not None:
        evidence.append("provider_geometry")
    return evidence


def _component_label_candidates(
    main_id: str | None,
    identifiers: tuple[str, ...],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    seen_label_values: set[tuple[str, str]] = set()
    values = []
    if main_id:
        values.append(("main_id", main_id, "medium"))
    values.extend(("identifier", value, "low") for value in identifiers)
    for source, value, confidence in values:
        label = _component_label_from_identifier(value)
        if label is None:
            continue
        label_value_key = (label, value)
        if label_value_key in seen_label_values:
            continue
        seen_label_values.add(label_value_key)
        key = (label, source, value)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "label": label,
            "source": source,
            "value": value,
            "confidence": confidence,
        })
    return candidates


def _component_label_from_identifier(value: str) -> str | None:
    text = " ".join(value.strip().split())
    if not text:
        return None
    matched = _WDS_CCDM_COMPONENT_RE.search(text)
    if matched:
        return _normalize_component_label(matched.group(1))
    # SIMBAD commonly writes HD component names without a separating space,
    # for example ``HD 224953A``. Keep this catalog-specific: a trailing
    # letter is not generally safe to interpret as a component label.
    matched = _HD_ATTACHED_COMPONENT_RE.fullmatch(text)
    if matched:
        return _normalize_component_label(matched.group(1))
    token = text.rsplit(" ", 1)[-1]
    if _COMPONENT_TOKEN_RE.fullmatch(token) and not _component_token_looks_like_catalog_suffix(text, token):
        return _normalize_component_label(token)
    matched = _TRAILING_COMPONENT_RE.search(text)
    if matched:
        return _normalize_component_label(matched.group(1))
    return None


def _component_token_looks_like_catalog_suffix(text: str, token: str) -> bool:
    if len(token) != 1:
        return False
    prefix = text[: -len(token)].rstrip()
    return bool(prefix and prefix[-1].isdigit() and " " not in prefix)


def _normalize_component_label(value: str) -> str:
    if len(value) >= 2 and value[0].isalpha() and value[1:].islower():
        return value[0].upper() + value[1:]
    return value.upper()


def _best_component_label_candidate(candidates: list[dict[str, object]]) -> str | None:
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.get("source") == "main_id":
            return str(candidate["label"])
    return str(candidates[0]["label"])


def _component_labels_match(first: str, second: str) -> bool:
    return _normalize_component_label(first) == _normalize_component_label(second)


def _component_label_is_group(value: str) -> bool:
    label = _normalize_component_label(value.strip())
    if not label:
        return False
    if "," in label:
        return True
    return len(label) > 1 and label.isalpha() and label.isupper()


def _component_label_contains(group: str, component: str) -> bool:
    group = _normalize_component_label(group)
    component = _normalize_component_label(component)
    if group == component:
        return True
    if "," in group:
        return any(_component_label_contains(part.strip(), component) for part in group.split(","))
    if len(component) == 1 and len(group) > 1 and group.isalpha() and group.isupper():
        return component in group
    if len(component) >= 1 and len(group) >= 1:
        return component[0] == group[0] and len(group) <= len(component)
    return False


def _target_component_assignment_summary(value: dict[str, object]) -> dict[str, object]:
    return {
        "status": value["status"],
        "confidence": value["confidence"],
        "semantic_kind": value["semantic_kind"],
        "semantic_component": value["semantic_component"],
        "nearest_component": value["nearest_component"],
        "nearest_separation_arcsec": value["nearest_separation_arcsec"],
        "review_required": value["review_required"],
    }


def _target_photometry_context(
    session: Session,
    target: Target,
    *,
    component_assignment: dict[str, object],
    closest_companion: dict[str, object] | None,
) -> dict[str, object]:
    measurements = current_measurements_for_target(session, target.id)
    measurements.sort(key=lambda value: (value.provider, value.band, value.id))
    nearest_pair_arcsec = (
        None if closest_companion is None
        else closest_companion.get("separation_from_nearest_arcsec")
    )
    assignment_status = str(component_assignment.get("status") or "")
    semantic_kind = str(component_assignment.get("semantic_kind") or "")
    if assignment_status == "system_level_target":
        target_level = "system"
    elif (
        assignment_status == "semantic_group_contains_nearest_component"
        and _component_label_is_group(str(component_assignment.get("semantic_component") or ""))
    ):
        target_level = "system"
    elif semantic_kind in {"component", "subsystem"}:
        target_level = "component_or_subsystem"
    elif closest_companion is not None:
        target_level = "hierarchical_context"
    else:
        target_level = "single_or_unknown"

    rows = []
    likely_blended = []
    predicted_scope_counts: dict[str, int] = {}
    predicted_blend_counts: dict[str, int] = {}
    unresolved_components = []
    if (
        nearest_pair_arcsec is not None
        and nearest_pair_arcsec <= 1.0
        and target_level == "system"
    ):
        unresolved_components = sorted({
            str(component_assignment.get("nearest_component") or ""),
            str(component_assignment.get("closest_companion_component") or ""),
        } - {""})
    for measurement in measurements:
        resolution = measurement.resolution_major_arcsec
        if resolution is not None and nearest_pair_arcsec is not None:
            if resolution >= nearest_pair_arcsec:
                blend_prediction = "likely_blended_at_catalog_resolution"
                likely_blended.append(f"{measurement.provider}:{measurement.band}")
            else:
                blend_prediction = "likely_resolved_at_catalog_resolution"
        elif nearest_pair_arcsec is None:
            blend_prediction = "no_nearby_component_estimate"
        else:
            blend_prediction = "unknown_resolution"
        scope_prediction = _photometry_scope_prediction(
            target_level=target_level,
            assignment_status=assignment_status,
            semantic_kind=semantic_kind,
            blend_prediction=blend_prediction,
            stored_ownership_scope=measurement.ownership_scope,
            stored_blend_state=measurement.blend_state,
        )
        predicted_scope_counts[scope_prediction["predicted_ownership_scope"]] = (
            predicted_scope_counts.get(scope_prediction["predicted_ownership_scope"], 0) + 1
        )
        predicted_blend_counts[scope_prediction["predicted_blend_state"]] = (
            predicted_blend_counts.get(scope_prediction["predicted_blend_state"], 0) + 1
        )
        rows.append({
            "provider": measurement.provider,
            "band": measurement.band,
            "resolution_major_arcsec": resolution,
            "resolution_minor_arcsec": measurement.resolution_minor_arcsec,
            "resolution_kind": measurement.resolution_kind,
            "resolution_reference": measurement.resolution_reference,
            "ownership_scope": measurement.ownership_scope,
            "stored_ownership_scope": measurement.ownership_scope,
            "stored_blend_state": measurement.blend_state,
            "stored_blend_reason": measurement.blend_reason,
            "resolution_blend_evidence": blend_prediction,
            **scope_prediction,
        })

    if not measurements:
        recommendation = "no current photometry measurements to assess"
    elif target_level == "system":
        recommendation = (
            "treat current photometry as system/subsystem-level unless a catalog-specific "
            "component association says otherwise"
        )
    elif likely_blended:
        recommendation = "review low-resolution bands before interpreting component-level excess"
    elif closest_companion is not None:
        recommendation = "hierarchy is present but current band resolutions do not obviously force blending"
    else:
        recommendation = "no hierarchy-driven photometry concern identified"

    return {
        "target_level": target_level,
        "nearest_pair_arcsec": nearest_pair_arcsec,
        "likely_unresolved_components": unresolved_components,
        "likely_blended_bands": likely_blended,
        "measurement_count": len(measurements),
        "predicted_scope_counts": dict(sorted(predicted_scope_counts.items())),
        "predicted_blend_counts": dict(sorted(predicted_blend_counts.items())),
        "bands": rows,
        "recommendation": recommendation,
        "review_required": bool(likely_blended and target_level != "system"),
    }


def _refresh_photometry_band_summaries(photometry: dict[str, object]) -> None:
    bands = list(photometry.get("bands") or [])
    scope_counts: dict[str, int] = {}
    blend_counts: dict[str, int] = {}
    for band in bands:
        scope = str(band.get("predicted_ownership_scope") or "unknown")
        blend = str(band.get("predicted_blend_state") or "unknown")
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        blend_counts[blend] = blend_counts.get(blend, 0) + 1
    photometry["predicted_scope_counts"] = dict(sorted(scope_counts.items()))
    photometry["predicted_blend_counts"] = dict(sorted(blend_counts.items()))
    photometry["review_required"] = bool(
        photometry.get("likely_blended_bands")
        and photometry.get("target_level") != "system"
    )


def _photometry_scope_prediction(
    *,
    target_level: str,
    assignment_status: str,
    semantic_kind: str,
    blend_prediction: str,
    stored_ownership_scope: str,
    stored_blend_state: str,
) -> dict[str, str]:
    if stored_ownership_scope != "component" or stored_blend_state != "clear":
        return {
            "predicted_ownership_scope": stored_ownership_scope,
            "predicted_blend_state": stored_blend_state,
            "predicted_blend_reason": "stored_catalog_state",
            "scope_reason": "provider or existing catalog state already marks this measurement",
        }
    if assignment_status in {
        "review_required",
        "semantic_geometry_conflict",
        "ambiguous_disconnected_groups",
    }:
        return {
            "predicted_ownership_scope": "ambiguous",
            "predicted_blend_state": "ambiguous",
            "predicted_blend_reason": "hierarchy_ambiguous",
            "scope_reason": f"target assignment is {assignment_status}",
        }
    if (
        target_level == "system"
        and blend_prediction == "likely_resolved_at_catalog_resolution"
    ):
        return {
            "predicted_ownership_scope": "component",
            "predicted_blend_state": "clear",
            "predicted_blend_reason": "resolved_at_catalog_resolution",
            "scope_reason": (
                "catalog resolution separates the nearest known components; "
                "the selected source is associated with the component at the target position"
            ),
        }
    if (
        target_level == "system"
        and blend_prediction == "likely_blended_at_catalog_resolution"
    ):
        return {
            "predicted_ownership_scope": "system",
            "predicted_blend_state": "blended",
            "predicted_blend_reason": "unresolved_at_catalog_resolution",
            "scope_reason": (
                "target is a system and catalog resolution is larger than the "
                "nearest known component separation"
            ),
        }
    if target_level == "system":
        return {
            "predicted_ownership_scope": "system",
            "predicted_blend_state": "unknown",
            "predicted_blend_reason": "system_level_target",
            "scope_reason": (
                "SIMBAD/provider context identifies the target as a system or parent, "
                "and catalog resolution does not distinguish a component"
            ),
        }
    if blend_prediction == "likely_blended_at_catalog_resolution":
        return {
            "predicted_ownership_scope": "shared",
            "predicted_blend_state": "blended",
            "predicted_blend_reason": "unresolved_at_catalog_resolution",
            "scope_reason": "catalog resolution is larger than the nearest known component separation",
        }
    if blend_prediction == "likely_resolved_at_catalog_resolution":
        return {
            "predicted_ownership_scope": "component",
            "predicted_blend_state": "clear",
            "predicted_blend_reason": "resolved_at_catalog_resolution",
            "scope_reason": "catalog resolution is smaller than the nearest known component separation",
        }
    if semantic_kind in {"component", "subsystem"}:
        return {
            "predicted_ownership_scope": "component",
            "predicted_blend_state": "unknown",
            "predicted_blend_reason": "unknown_resolution",
            "scope_reason": "target is semantically component-like, but band resolution is unavailable",
        }
    return {
        "predicted_ownership_scope": "component",
        "predicted_blend_state": "unknown",
        "predicted_blend_reason": blend_prediction,
        "scope_reason": "no hierarchy/resolution evidence changes component-level interpretation",
    }


def _target_photometry_context_summary(value: dict[str, object]) -> dict[str, object]:
    bands = value.get("bands") or []
    return {
        "target_level": value.get("target_level"),
        "nearest_pair_arcsec": value.get("nearest_pair_arcsec"),
        "measurement_count": value.get("measurement_count", 0),
        "likely_blended_bands": value.get("likely_blended_bands", []),
        "likely_unresolved_components": value.get("likely_unresolved_components", []),
        "predicted_scope_counts": value.get("predicted_scope_counts", {}),
        "predicted_blend_counts": value.get("predicted_blend_counts", {}),
        "bands_with_resolution": sum(
            1 for band in bands
            if band.get("resolution_major_arcsec") is not None
        ),
        "recommendation": value.get("recommendation"),
        "review_required": value.get("review_required", False),
    }


_REVIEW_PRIORITY_RANKS = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "highest": 4,
}


def _review_priority_rank(value: str) -> int:
    key = value.lower().strip()
    if key not in _REVIEW_PRIORITY_RANKS:
        raise ValueError(f"unknown review priority: {value}")
    return _REVIEW_PRIORITY_RANKS[key]


def _review_queue_row(
    context: dict[str, object],
    photometry: dict[str, object],
) -> dict[str, object]:
    systems = list(context["systems"])
    candidate_count = sum(len(system["candidates"]) for system in systems)
    accepted_count = sum(
        1
        for system in systems
        for candidate in system["candidates"]
        if candidate["status"] == "accepted"
    )
    rejected_count = sum(
        1
        for system in systems
        for candidate in system["candidates"]
        if candidate["status"] == "rejected"
    )
    diagnostic_count = sum(len(system["diagnostics"]) for system in systems)
    candidate_system_count = len(systems)
    basis = str(context["hierarchy_decision_basis"])
    likely_blended = list(photometry["likely_blended_bands"])
    assignment = context["component_assignment"]
    assignment_status = str(assignment["status"])
    review_required = bool(context["review_required"] or photometry["review_required"])

    if likely_blended and basis == "candidate_review":
        priority = "highest"
        reason = "likely blended photometry depends on unaccepted hierarchy candidates"
    elif accepted_count == 0 and candidate_system_count > 1:
        priority = "high"
        reason = "multiple candidate hierarchy systems need a decision"
    elif assignment_status == "semantic_geometry_conflict":
        priority = "high"
        reason = "SIMBAD semantic component and provider geometry disagree"
    elif review_required or diagnostic_count:
        priority = "medium"
        reason = "hierarchy diagnostics or photometry context require review"
    elif candidate_count and accepted_count == 0:
        priority = "low"
        reason = "single clean hierarchy candidate has not been accepted"
    elif accepted_count:
        priority = "low"
        reason = "accepted hierarchy decision present"
    else:
        priority = "none"
        reason = "no hierarchy review item"

    sdbid = str(context["target"]["sdbid"])
    return {
        "sdbid": sdbid,
        "priority": priority,
        "reason": reason,
        "candidate_count": candidate_count,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "candidate_system_count": candidate_system_count,
        "diagnostic_count": diagnostic_count,
        "basis": basis,
        "classification": context["classification"],
        "component_assignment_status": assignment_status,
        "likely_blended_bands": likely_blended,
        "nearest_pair_arcsec": photometry["nearest_pair_arcsec"],
        "review_required": review_required,
        "review_view_hint": f"sdb review-view {sdbid} --output {sdbid}-review.html",
    }


def _target_semantic_identity(session: Session, target: Target) -> dict[str, object]:
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
        return {
            "kind": "unknown",
            "evidence": "no_current_simbad_metadata",
            "confidence": "none",
            "status": "missing",
            "main_id": None,
            "parents": [],
            "children": [],
        }
    if run.status != "match":
        return {
            "kind": "unknown",
            "evidence": "simbad_metadata_status",
            "confidence": "none",
            "status": run.status,
            "main_id": None,
            "parents": [],
            "children": [],
        }
    metadata = session.scalar(
        select(SimbadMetadata)
        .where(SimbadMetadata.run_id == run.id)
        .limit(1)
    )
    simbad_identifiers = tuple(session.scalars(
        select(ExternalIdentifier.value)
        .where(
            ExternalIdentifier.target_id == target.id,
            ExternalIdentifier.source.in_(("simbad_metadata", "simbad")),
        )
        .order_by(ExternalIdentifier.id)
    ))
    component_label_candidates = _component_label_candidates(
        None if metadata is None else metadata.main_id,
        simbad_identifiers,
    )
    relationships = tuple(session.scalars(
        select(SimbadRelationship)
        .where(SimbadRelationship.run_id == run.id)
        .order_by(
            SimbadRelationship.direction,
            SimbadRelationship.separation_arcsec,
            SimbadRelationship.related_main_id,
        )
    ))
    parents = [
        _target_semantic_relationship(row)
        for row in relationships
        if row.direction == "parent"
    ]
    children = [
        _target_semantic_relationship(row)
        for row in relationships
        if row.direction == "child"
    ]
    relevance_counts = _semantic_relevance_counts([*parents, *children])
    structural_parents = _structural_simbad_relationships(parents)
    structural_children = _structural_simbad_relationships(children)
    if structural_parents and structural_children:
        kind = "subsystem"
    elif structural_parents:
        kind = "component"
    elif structural_children:
        kind = "system_or_parent"
    else:
        kind = "single_or_no_known_hierarchy"
    return {
        "kind": kind,
        "evidence": "simbad_relationships",
        "confidence": "high" if parents or children else "medium",
        "status": run.status,
        "run_id": run.id,
        "main_id": None if metadata is None else metadata.main_id,
        "oid": None if metadata is None else metadata.oid,
        "primary_object_type": None if metadata is None else metadata.primary_object_type,
        "component_label_candidates": component_label_candidates,
        "relationship_relevance_counts": relevance_counts,
        "parents": parents,
        "children": children,
    }


def _structural_simbad_relationships(
    relationships: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        value for value in relationships
        if value.get("component_relevance") == "stellar_or_substellar_component"
    ]


def _semantic_relevance_counts(relationships: list[dict[str, object]]) -> dict[str, int]:
    counts = {
        "stellar_or_substellar_component": 0,
        "planetary_or_disk": 0,
        "contextual_group": 0,
        "unknown": 0,
    }
    for value in relationships:
        relevance = str(value.get("component_relevance") or "unknown")
        counts[relevance] = counts.get(relevance, 0) + 1
    return counts


def _target_semantic_relationship(value: SimbadRelationship) -> dict[str, object]:
    object_types = json.loads(value.related_object_types_json or "[]")
    relevance = _simbad_component_relevance(value.related_object_type, object_types)
    return {
        "related_oid": value.related_oid,
        "main_id": value.related_main_id,
        "ra_deg": value.related_ra_deg,
        "dec_deg": value.related_dec_deg,
        "object_type": value.related_object_type,
        "object_types": object_types,
        "component_relevance": relevance,
        "spectral_type": value.related_spectral_type,
        "spectral_type_bibcode": value.related_spectral_type_bibcode,
        "membership_percent": value.membership_percent,
        "bibcode": value.link_bibcode,
        "separation_arcsec": value.separation_arcsec,
    }


def _simbad_component_relevance(
    primary_type: str | None,
    object_types: list[str],
) -> str:
    codes = {
        _normalize_simbad_type(value)
        for value in [primary_type, *object_types]
        if value
    }
    if not codes:
        return "unknown"
    if codes & _SIMBAD_PLANETARY_OR_DISK_TYPES:
        return "planetary_or_disk"
    if codes & _SIMBAD_CONTEXTUAL_GROUP_TYPES:
        return "contextual_group"
    if any(_simbad_type_is_stellar_or_substellar(code) for code in codes):
        return "stellar_or_substellar_component"
    return "unknown"


def _normalize_simbad_type(value: str) -> str:
    return value.strip().lower()


def _simbad_type_is_stellar_or_substellar(code: str) -> bool:
    if "*" in code:
        return True
    return code in {
        "star",
        "bd",
        "bd?",
        "brown dwarf",
        "low-mass*",
    }


_SIMBAD_PLANETARY_OR_DISK_TYPES = {
    "pl",
    "pl?",
    "planet",
    "exoplanet",
    "disk",
    "debrisdisk",
    "debris disk",
    "protoplanetarydisk",
    "protoplanetary disk",
}


_SIMBAD_CONTEXTUAL_GROUP_TYPES = {
    "cl*",
    "assoc*",
    "as*",
    "assoc",
    "association",
    "mgr",
    "moving group",
    "cluster",
    "open cluster",
    "globular cluster",
    "region",
    "hii",
    "molcld",
    "cloud",
    "neb",
    "nebula",
}


def _target_semantic_identity_summary(value: dict[str, object]) -> dict[str, object]:
    return {
        "kind": value["kind"],
        "evidence": value["evidence"],
        "confidence": value["confidence"],
        "status": value["status"],
        "main_id": value["main_id"],
        "component_label_candidates": value.get("component_label_candidates", []),
        "parents": len(value["parents"]),
        "children": len(value["children"]),
        "relationship_relevance_counts": value.get("relationship_relevance_counts", {}),
    }


def _find_required_target(session: Session, reference: str | int | None) -> Target:
    if reference is None:
        raise ValueError("target reference is required")
    target = find_target(session, reference)
    if target is None:
        raise KeyError(f"target not found: {reference}")
    return target


def _find_required_system(session: Session, name: str | None) -> TargetSystem:
    if name is None or not name.strip():
        raise ValueError("system name is required")
    system = session.scalar(select(TargetSystem).where(TargetSystem.name == name.strip()))
    if system is None:
        raise KeyError(f"system not found: {name}")
    return system


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
    system = session.scalar(select(TargetSystem).where(TargetSystem.name == clean_name))
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
        )
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


def _find_required_candidate_context(
    session: Session,
    candidate_id: int,
) -> tuple[HierarchyMatchCandidate, HierarchyRecord, Target]:
    row = session.execute(
        select(HierarchyMatchCandidate, HierarchyRecord, Target)
        .join(HierarchyRecord, HierarchyRecord.id == HierarchyMatchCandidate.record_id)
        .join(Target, Target.id == HierarchyMatchCandidate.target_id)
        .where(HierarchyMatchCandidate.id == candidate_id)
    ).one_or_none()
    if row is None:
        raise KeyError(f"hierarchy match candidate not found: {candidate_id}")
    candidate, record, target = row
    return candidate, record, target


def _target_sdbid(session: Session, target_id: int | None) -> str | None:
    if target_id is None:
        return None
    target = session.get(Target, target_id)
    return None if target is None else target.sdbid


def _relationship_summary(session: Session, value: StructuralEdge) -> RelationshipSummary:
    parent_id = child_id = primary_id = secondary_id = None
    if value.direction == "a_parent_b":
        parent_id, child_id = value.endpoint_a_target_id, value.endpoint_b_target_id
    elif value.direction == "b_parent_a":
        parent_id, child_id = value.endpoint_b_target_id, value.endpoint_a_target_id
    else:
        primary_id, secondary_id = value.endpoint_a_target_id, value.endpoint_b_target_id
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


def _hierarchy_candidates_for_record(
    record: HierarchyRecord,
    radius_arcsec: float,
    alias_index: dict[str, tuple[tuple[str, Target], ...]],
    target_index: dict[int, tuple[Target, ...]],
    *,
    components_by_system: dict[tuple[str, int, str], frozenset[str]] | None = None,
) -> dict[int, dict[str, object]]:
    raw_payload = _record_raw_payload(record)
    if _wds_record_has_unusable_separation(record, raw_payload=raw_payload):
        return {}
    candidates: dict[int, dict[str, object]] = {}
    for identifier in _identifier_variants(
        record,
        components_by_system=components_by_system,
    ):
        normalized = normalize_identifier(identifier)
        if not normalized:
            continue
        for alias_value, target in alias_index.get(normalized, ()):
            candidate = _candidate_entry(candidates, target.id)
            candidate["methods"].add("identifier")
            candidate["score"] = max(float(candidate["score"]), 1.0)
            if candidate["identifier"] is None:
                candidate["identifier"] = alias_value
            candidate["reasons"].add(f"identifier match: {alias_value}")
            separation_values = _target_separations(
                record, target, raw_payload=raw_payload,
            )
            if separation_values:
                separation, position_kind = min(separation_values, key=lambda item: item[0])
                candidate["separation_arcsec"] = _best_separation(candidate["separation_arcsec"], separation)
                if separation <= radius_arcsec:
                    candidate["methods"].add("position")
                    candidate["reasons"].add(f"{position_kind} separation {separation:.3f} arcsec")
                else:
                    candidate["reasons"].add(f"{position_kind} offset {separation:.3f} arcsec")
    if _record_positions(record, raw_payload=raw_payload):
        for target, separation, position_kind in _targets_near_record(
            record, radius_arcsec, target_index, raw_payload=raw_payload,
        ):
            candidate = _candidate_entry(candidates, target.id)
            candidate["methods"].add("position")
            candidate["score"] = max(
                float(candidate["score"]),
                max(0.0, 0.95 * (1.0 - separation / radius_arcsec)),
            )
            candidate["separation_arcsec"] = _best_separation(
                candidate["separation_arcsec"], separation,
            )
            candidate["reasons"].add(f"{position_kind} separation {separation:.3f} arcsec")
    return {
        target_id: {
            "methods": tuple(sorted(value["methods"])),
            "score": value["score"],
            "separation_arcsec": value["separation_arcsec"],
            "identifier": value["identifier"],
            "reasons": tuple(sorted(value["reasons"])),
        }
        for target_id, value in candidates.items()
    }


def _candidate_entry(candidates: dict[int, dict[str, object]], target_id: int) -> dict[str, object]:
    if target_id not in candidates:
        candidates[target_id] = {
            "methods": set(),
            "score": 0.0,
            "separation_arcsec": None,
            "identifier": None,
            "reasons": set(),
        }
    return candidates[target_id]


def _build_alias_index(session: Session) -> dict[str, tuple[tuple[str, Target], ...]]:
    values: dict[str, list[tuple[str, Target]]] = {}
    rows = session.execute(
        select(ExternalIdentifier, Target)
        .join(Target, Target.id == ExternalIdentifier.target_id)
        .order_by(Target.sdbid, ExternalIdentifier.value)
    )
    for alias, target in rows:
        values.setdefault(alias.normalized_value, []).append((alias.value, target))
    return {key: tuple(value) for key, value in values.items()}


def _build_target_index(targets: tuple[Target, ...]) -> dict[int, tuple[Target, ...]]:
    values: dict[int, list[Target]] = {}
    for target in targets:
        values.setdefault(math.floor(target.dec2000_deg), []).append(target)
    return {key: tuple(sorted(value, key=lambda target: target.sdbid)) for key, value in values.items()}


def _components_by_system(
    records: tuple[HierarchyRecord, ...],
) -> dict[tuple[str, int, str], frozenset[str]]:
    values: dict[tuple[str, int, str], set[str]] = {}
    for record in records:
        if not record.native_id:
            continue
        component = (record.component or "").strip().replace(" ", "")
        if not component:
            continue
        values.setdefault((record.provider, record.source_id, record.native_id), set()).add(component)
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
            (record.provider, record.source_id, native),
            frozenset(),
        )
        component_variants = _component_identifier_variants(
            component,
            sibling_components=sibling_components,
        )
        if (
            record.provider == "wds"
            and not component_variants
            and _blank_wds_record_implies_ab_identifier(
                record,
                components_by_system=components_by_system,
            )
        ):
            component_variants = ("AB",)
        if record.provider == "wds":
            values.append(f"WDS J{native}")
            for component_variant in component_variants:
                values.append(f"WDS J{native}{component_variant}")
        elif record.provider == "ccdm":
            coordinate_id = native[1:] if native.upper().startswith("J") else native
            values.extend([
                f"CCDM J{coordinate_id}",
                f"CCDM {coordinate_id}",
                f"WDS J{coordinate_id}",
            ])
            for component_variant in component_variants:
                values.extend([
                    f"CCDM J{coordinate_id}{component_variant}",
                    f"CCDM {coordinate_id}{component_variant}",
                    f"WDS J{coordinate_id}{component_variant}",
                ])
    if discoverer:
        discoverer_values = [discoverer, _spaced_designation(discoverer)]
        values.extend(discoverer_values)
        if component:
            compact_component = component.replace(" ", "")
            for discoverer_value in discoverer_values:
                values.append(f"{discoverer_value} {compact_component}")
                values.append(f"{discoverer_value}{compact_component}")
    return tuple(dict.fromkeys(value for value in values if value.strip()))


def _blank_wds_record_implies_ab_identifier(
    record: HierarchyRecord,
    *,
    components_by_system: dict[tuple[str, int, str], frozenset[str]] | None,
) -> bool:
    if record.provider != "wds":
        return False
    if (record.component or "").strip():
        return False
    if not record.native_id:
        return False
    if record.separation_arcsec is None or record.pa_deg is None:
        return False
    if record.separation_arcsec <= 0:
        return False
    explicit = (components_by_system or {}).get(
        (record.provider, record.source_id, record.native_id),
        frozenset(),
    )
    return "AB" not in explicit


def _spaced_designation(value: str) -> str:
    return re.sub(r"^([A-Za-z]+)(\d+)$", r"\1 \2", value.strip())


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
    if len(parents) != 1:
        return None
    return next(iter(parents))


def _targets_near_record(
    record: HierarchyRecord,
    radius_arcsec: float,
    target_index: dict[int, tuple[Target, ...]],
    *,
    raw_payload: dict[str, object] | None = None,
) -> tuple[tuple[Target, float, str], ...]:
    positions = tuple(
        position for position in _record_positions(record, raw_payload=raw_payload)
        if _position_usable_for_matching(position[2])
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
                separation = _separation_arcsec(ra_deg, dec_deg, target.ra2000_deg, target.dec2000_deg)
                if separation <= radius_arcsec:
                    existing = rows.get(target.id)
                    if existing is None or separation < existing[1]:
                        rows[target.id] = (target, separation, position_kind)
    return tuple(sorted(rows.values(), key=lambda item: (item[1], item[0].sdbid)))


def _ra_within(center_deg: float, value_deg: float, half_width_deg: float) -> bool:
    if half_width_deg >= 180:
        return True
    delta = abs((value_deg - center_deg + 180.0) % 360.0 - 180.0)
    return delta <= half_width_deg


def _target_separations(
    record: HierarchyRecord,
    target: Target,
    *,
    raw_payload: dict[str, object] | None = None,
) -> tuple[tuple[float, str], ...]:
    return tuple(
        (
            _separation_arcsec(ra_deg, dec_deg, target.ra2000_deg, target.dec2000_deg),
            position_kind,
        )
        for ra_deg, dec_deg, position_kind in _record_positions(
            record, raw_payload=raw_payload,
        )
        if _position_usable_for_matching(position_kind)
    )


def hierarchy_record_positions(record: HierarchyRecord) -> tuple[tuple[float, float, str], ...]:
    """Return useful sky positions for matching/reviewing a hierarchy record.

    The catalog coordinate is usually the system/pair base position.  If a
    separation and position angle are present, the endpoint is the component or
    pair-secondary position implied by the catalog geometry.  Both are useful:
    older WDS/CCDM-style coordinates can be coarse, while component endpoints
    are often the relevant location for resolved targets.
    """
    return _record_positions(record)


def _record_positions(
    record: HierarchyRecord,
    *,
    raw_payload: dict[str, object] | None = None,
) -> tuple[tuple[float, float, str], ...]:
    if record.ra_deg is None or record.dec_deg is None:
        return ()
    if raw_payload is None:
        raw_payload = _record_raw_payload(record)
    values = [(record.ra_deg, record.dec_deg, _record_position_kind(record, raw_payload))]
    if (
        record.separation_arcsec is not None
        and record.pa_deg is not None
        and _hierarchy_separation_usable(record.provider, record.separation_arcsec)
    ):
        endpoint = _offset_position(
            record.ra_deg,
            record.dec_deg,
            record.separation_arcsec,
            record.pa_deg,
        )
        values.append((endpoint[0], endpoint[1], "component endpoint"))
    return tuple(values)


def _wds_record_has_unusable_separation(
    record: HierarchyRecord,
    *,
    raw_payload: dict[str, object] | None = None,
) -> bool:
    if record.provider != "wds":
        return False
    if record.separation_arcsec is not None and record.separation_arcsec >= WDS_UNUSABLE_SEPARATION_ARCSEC:
        return True
    if raw_payload is None:
        raw_payload = _record_raw_payload(record)
    return raw_payload.get("unusable_separation_arcsec") is not None


def _position_usable_for_matching(position_kind: str) -> bool:
    return position_kind not in {
        "coarse CCDM identifier position",
        "coarse WDS identifier position",
        "low-quality catalog position",
    }


def _record_position_kind(record: HierarchyRecord, raw_payload: dict[str, object]) -> str:
    coordinate_source = str(raw_payload.get("coordinate_source") or "")
    if coordinate_source == "ccdm_id_only":
        return "coarse CCDM identifier position"
    if coordinate_source == "wds_id_only":
        return "coarse WDS identifier position"
    coo_flag = raw_payload.get("CooFlag")
    try:
        if coo_flag is not None and int(coo_flag) > 0:
            return "low-quality catalog position"
    except (TypeError, ValueError):
        pass
    return "record position"


def _hierarchy_separation_usable(provider: str, separation_arcsec: float | None) -> bool:
    if separation_arcsec is None:
        return False
    if provider == "wds" and separation_arcsec >= WDS_UNUSABLE_SEPARATION_ARCSEC:
        return False
    return True


def _record_raw_payload(record: HierarchyRecord) -> dict[str, object]:
    try:
        value = json.loads(record.raw_payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _offset_position(
    ra_deg: float,
    dec_deg: float,
    separation_arcsec: float,
    pa_deg: float,
) -> tuple[float, float]:
    pa = math.radians(pa_deg)
    east_arcsec = separation_arcsec * math.sin(pa)
    north_arcsec = separation_arcsec * math.cos(pa)
    cos_dec = max(0.01, abs(math.cos(math.radians(dec_deg))))
    return (
        (ra_deg + east_arcsec / (3600.0 * cos_dec)) % 360.0,
        dec_deg + north_arcsec / 3600.0,
    )


def _separation_arcsec(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    ra1 = math.radians(ra1_deg)
    dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg)
    dec2 = math.radians(dec2_deg)
    cosine = (
        math.sin(dec1) * math.sin(dec2)
        + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine)))) * 3600.0


def _best_separation(current: object, new: float) -> float:
    if current is None:
        return new
    return min(float(current), new)


def _parse_hierarchy_snapshot(provider: str, text: str) -> tuple[list[ParsedHierarchyRecord], int]:
    rows = _parse_delimited_snapshot(provider, text)
    if rows is None:
        rows = [
            _parse_wds_fixed(line) if provider == "wds" else _parse_ccdm_fixed(line)
            for line in text.splitlines()
        ]
    parsed = [row for row in rows if row is not None]
    return parsed, len(rows) - len(parsed)


def _parse_hierarchy_tables(provider: str, tables) -> list[ParsedHierarchyRecord]:
    parsed = []
    for index, table in enumerate(tables):
        table_name = _astropy_table_name(table, index)
        if not _parseable_hierarchy_table(provider, table_name, getattr(table, "meta", {}) or {}):
            continue
        for row in table:
            payload = row_payload(row)
            record = _parse_mapping_record(provider, {
                str(key): "" if value is None else str(value)
                for key, value in payload.items()
            })
            if record is not None:
                parsed.append(record)
    return parsed


def _parse_cached_hierarchy_snapshot(
    provider: str,
    cached: CachedSnapshotData,
) -> list[ParsedHierarchyRecord]:
    parsed = []
    for table in cached.tables:
        if not _parseable_hierarchy_table(provider, table.name, table.metadata):
            continue
        for payload in table.rows:
            record = _parse_mapping_record(provider, {
                str(key): "" if value is None else str(value)
                for key, value in payload.items()
            })
            if record is not None:
                parsed.append(record)
    return parsed


def _astropy_table_name(table, index: int) -> str:
    meta = getattr(table, "meta", {}) or {}
    return str(meta.get("name") or meta.get("ID") or f"table{index + 1}")


def _parseable_hierarchy_table(
    provider: str,
    table_name: str,
    metadata: dict[str, object] | None = None,
) -> bool:
    allowed = HIERARCHY_MAIN_TABLES.get(provider)
    if not allowed:
        return True
    names = {
        table_name.strip().lower(),
        str((metadata or {}).get("name") or "").strip().lower(),
        str((metadata or {}).get("ID") or "").strip().lower(),
    }
    # VizieR catalog snapshots often include auxiliary notes/reference tables
    # with target IDs but no usable source rows.  Keep those tables in the raw
    # cache, but only parse the configured main table into hierarchy records.
    return any(name in allowed for name in names)


def _release_from_readme(provider: str, catalog: str, readme: str) -> str:
    date_match = re.search(
        r"(?:version|updated|last\s+update|date)\D{0,30}"
        r"((?:19|20)\d{2}[-/][A-Za-z0-9]{1,3}[-/][A-Za-z0-9]{1,4}|"
        r"[A-Za-z]{3,9}\s+\d{1,2},?\s+(?:19|20)\d{2}|"
        r"(?:19|20)\d{2}\.\d+)",
        readme,
        flags=re.IGNORECASE,
    )
    if date_match:
        return f"{provider}:{catalog}:{date_match.group(1).strip()}"
    digest = hashlib.sha256(readme.encode()).hexdigest()[:12]
    return f"{provider}:{catalog}:readme-{digest}"


def _readme_version_note(readme: str) -> str | None:
    for line in readme.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ("version", "updated", "last update", "date")):
            return line.strip()
    return None


def _join_notes(*values: str | None) -> str | None:
    notes = [value.strip() for value in values if value and value.strip()]
    return "; ".join(notes) if notes else None


def _chunks(values: list[int], size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _parse_delimited_snapshot(provider: str, text: str) -> list[ParsedHierarchyRecord | None] | None:
    sample_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]
    if not sample_lines:
        return []
    delimiter = "\t" if "\t" in sample_lines[0] else ("," if "," in sample_lines[0] else None)
    if delimiter is None:
        return None
    reader = csv.DictReader(sample_lines, delimiter=delimiter)
    if not reader.fieldnames:
        return []
    return [_parse_mapping_record(provider, row) for row in reader]


def _parse_mapping_record(provider: str, row: dict[str, str]) -> ParsedHierarchyRecord | None:
    lowered = {key.lower().strip(): value.strip() for key, value in row.items() if key is not None}
    if provider == "wds" and _wds_dubious_notes(_first_text(
        lowered, "notes", "note", "n_notes", "n", "rem", "remarks",
    )):
        return None
    native_id = _first_text(lowered, "wds", "wdsid", "ccdm", "id", "name", "native_id")
    if provider == "ccdm" and native_id and native_id.upper().startswith("CCDM "):
        native_id = native_id[5:].strip()
    if not native_id:
        return None
    component = _first_text(lowered, "comp", "component", "components", "m_ccdm")
    discoverer = _first_text(lowered, "disc", "discov", "discoverer", "discoverer_id")
    ra = _first_ra_deg(lowered, "ra_deg", "raj2000", "_raj2000", "ra_icrs", "ra", "_ra.icrs")
    dec = _first_dec_deg(lowered, "dec_deg", "dej2000", "_dej2000", "de_icrs", "dec", "de", "_de.icrs")
    explicit_position = ra is not None and dec is not None
    if ra is None or dec is None:
        ra, dec = _coords_from_hierarchy_id(native_id)
    first_epoch = _first_float(lowered, "first", "first_epoch", "obs1", "date1", "ep1")
    last_epoch = _first_float(lowered, "last", "last_epoch", "obs2", "date2", "ep2")
    measure_epoch = _first_float(lowered, "epoch", "measure_epoch", "last_epoch") or last_epoch
    if provider == "wds":
        # WDS VizieR rows expose first/last measures separately.  Use the most
        # recent pair geometry for hierarchy matching/review; the catalog
        # coordinate is the primary/reference position, not the secondary
        # endpoint.
        separation = _first_float(
            lowered, "sep2", "rho2", "lastsep", "sep", "rho",
            "separation", "separation_arcsec",
        )
        pa = _first_float(
            lowered, "pa2", "theta2", "lastpa", "pa", "theta", "posang", "pa_deg",
        )
    else:
        separation = _first_float(lowered, "sep", "sep2", "rho", "separation", "separation_arcsec")
        pa = _first_float(lowered, "pa", "pa2", "theta", "posang", "pa_deg")
    raw_payload: dict[str, object] = dict(row)
    if provider == "wds" and not _hierarchy_separation_usable(provider, separation):
        if separation is not None:
            raw_payload["unusable_separation_arcsec"] = separation
            raw_payload["unusable_separation_reason"] = "WDS 999.9 separation sentinel"
        separation = None
        pa = None
    mag1 = _first_float(lowered, "mag1", "m1", "magra", "v1")
    mag2 = _first_float(lowered, "mag2", "m2", "magb", "v2")
    delta_mag = _first_float(lowered, "dmag", "delta_mag")
    if delta_mag is None and mag1 is not None and mag2 is not None:
        delta_mag = _delta_mag(mag1, mag2)
    if provider == "wds":
        reference_component, concerned_component = _wds_component_pair(component)
        raw_payload.update({
            "rComp": reference_component or "",
            "Comp": concerned_component or component or "",
            "component_label": component or "",
            "coordinate_source": "wds_catalog" if explicit_position else "wds_id_only",
        })
    return ParsedHierarchyRecord(
        native_id=native_id,
        component=component,
        discoverer_id=discoverer,
        ra_deg=ra,
        dec_deg=dec,
        first_epoch=first_epoch,
        last_epoch=last_epoch,
        measure_epoch=measure_epoch,
        separation_arcsec=separation,
        pa_deg=pa,
        magnitude_primary=mag1,
        magnitude_secondary=mag2,
        delta_mag=delta_mag,
        raw_payload=raw_payload,
    )


def _parse_wds_fixed(line: str) -> ParsedHierarchyRecord | None:
    if not line.strip() or line.lstrip().startswith(("#", ";")):
        return None
    if _wds_dubious_notes(_wds_fixed_notes(line)):
        return None
    native_id = line[0:10].strip()
    if len(native_id) < 10 or not native_id[:5].isdigit():
        return None
    discoverer = line[10:17].strip() or None
    component = line[17:22].strip() or None
    first_epoch = _float_text(line[23:27])
    last_epoch = _float_text(line[28:32])
    pa = _float_text(line[42:45]) or _float_text(line[38:41])
    separation = _float_text(line[52:62]) or _float_text(line[46:51])
    mag1 = _float_text(line[63:68])
    mag2 = _float_text(line[69:74])
    if (last_epoch is not None and last_epoch < 1000) or separation is None:
        compact = _parse_wds_compact(line)
        if compact is not None:
            return compact
    ra, dec = _coords_from_hierarchy_id(native_id)
    reference_component, concerned_component = _wds_component_pair(component)
    raw_payload: dict[str, object] = {
        "line": line,
        "rComp": reference_component or "",
        "Comp": concerned_component or component or "",
        "component_label": component or "",
        "coordinate_source": "wds_id_only",
    }
    if not _hierarchy_separation_usable("wds", separation):
        if separation is not None:
            raw_payload["unusable_separation_arcsec"] = separation
            raw_payload["unusable_separation_reason"] = "WDS 999.9 separation sentinel"
        separation = None
        pa = None
    return ParsedHierarchyRecord(
        native_id=native_id,
        component=component,
        discoverer_id=discoverer,
        ra_deg=ra,
        dec_deg=dec,
        first_epoch=first_epoch,
        last_epoch=last_epoch,
        measure_epoch=last_epoch,
        separation_arcsec=separation,
        pa_deg=pa,
        magnitude_primary=mag1,
        magnitude_secondary=mag2,
        delta_mag=_delta_mag(mag1, mag2),
        raw_payload=raw_payload,
    )


def _parse_wds_compact(line: str) -> ParsedHierarchyRecord | None:
    tokens = line.split()
    if not tokens:
        return None
    first = tokens[0]
    native_id = first[:10]
    if len(native_id) < 10 or not native_id[:5].isdigit():
        return None
    discoverer = first[10:] or None
    offset = 1
    component = None
    if len(tokens) > offset and any(character.isalpha() for character in tokens[offset]):
        component = tokens[offset]
        offset += 1
    first_epoch = _float_token(tokens, offset)
    last_epoch = _float_token(tokens, offset + 1)
    pa = _float_token(tokens, offset + 4) or _float_token(tokens, offset + 3)
    separation = _float_token(tokens, offset + 6) or _float_token(tokens, offset + 5)
    mag1 = _float_token(tokens, offset + 7)
    mag2 = _float_token(tokens, offset + 8)
    ra, dec = _coords_from_hierarchy_id(native_id)
    reference_component, concerned_component = _wds_component_pair(component)
    raw_payload = {
        "line": line,
        "rComp": reference_component or "",
        "Comp": concerned_component or component or "",
        "component_label": component or "",
        "coordinate_source": "wds_id_only",
    }
    if not _hierarchy_separation_usable("wds", separation):
        if separation is not None:
            raw_payload["unusable_separation_arcsec"] = separation
            raw_payload["unusable_separation_reason"] = "WDS 999.9 separation sentinel"
        separation = None
        pa = None
    return ParsedHierarchyRecord(
        native_id=native_id,
        component=component,
        discoverer_id=discoverer,
        ra_deg=ra,
        dec_deg=dec,
        first_epoch=first_epoch,
        last_epoch=last_epoch,
        measure_epoch=last_epoch,
        separation_arcsec=separation,
        pa_deg=pa,
        magnitude_primary=mag1,
        magnitude_secondary=mag2,
        delta_mag=_delta_mag(mag1, mag2),
        raw_payload=raw_payload,
    )


def _wds_dubious_notes(value: str | None) -> bool:
    return "X" in (value or "").upper()


def _wds_fixed_notes(line: str) -> str | None:
    # WDS fixed-width rows place note flags after the photometric/spectral
    # columns in the tail of the row. Keep this deliberately broad: the compact
    # fallback format has no reliable note field, while standard long rows do.
    return line[107:].strip() if len(line) > 107 else None


def _wds_component_pair(component: str | None) -> tuple[str | None, str | None]:
    text = (component or "").strip()
    if not text:
        return None, None
    if "," in text:
        left, right = [part.strip() for part in text.split(",", 1)]
        return left or None, right or None
    compact = text.replace(" ", "")
    if len(compact) == 2 and compact.isalpha():
        return compact[0], compact[1]
    return None, compact or None


def _build_wds_record_index(records: tuple[HierarchyRecord, ...]) -> dict[tuple[int, str, str], HierarchyRecord]:
    index: dict[tuple[int, str, str], HierarchyRecord] = {}
    for record in records:
        if record.provider != "wds":
            continue
        component = (record.component or "").strip()
        if component:
            index.setdefault((record.source_id, record.native_id, component), record)
    return index


def _graph_edge_key(edge: StructuralEdge) -> tuple[int, str, str, str | None, str | None, str]:
    return (
        edge.source_id,
        edge.source,
        edge.native_id,
        edge.reference_label,
        edge.component_label,
        edge.relation_type,
    )


def _copy_graph_edge_values(existing: StructuralEdge, replacement: StructuralEdge) -> None:
    existing.record_id = replacement.record_id
    existing.source_component = replacement.source_component
    existing.structural_role = replacement.structural_role
    existing.status = replacement.status
    existing.geometry_status = replacement.geometry_status
    existing.start_ra_deg = replacement.start_ra_deg
    existing.start_dec_deg = replacement.start_dec_deg
    existing.end_ra_deg = replacement.end_ra_deg
    existing.end_dec_deg = replacement.end_dec_deg
    existing.separation_arcsec = replacement.separation_arcsec
    existing.pa_deg = replacement.pa_deg
    existing.relation_epoch = replacement.relation_epoch
    existing.note = replacement.note


def _wds_graph_edge_for_record(
    record: HierarchyRecord,
    record_index: dict[tuple[int, str, str], HierarchyRecord],
) -> StructuralEdge | None:
    if record.provider != "wds":
        return None
    raw_payload = _record_raw_payload(record)
    if _wds_record_has_unusable_separation(record, raw_payload=raw_payload):
        return None
    source_component = (record.component or "").strip() or None
    reference, component = _wds_graph_component_pair(record, record_index)
    if not reference or not component:
        return None
    start = _wds_graph_start_position(record, record_index, reference)
    end: tuple[float, float] | None = None
    geometry_status = "missing"
    if (
        start is not None
        and record.separation_arcsec is not None
        and record.pa_deg is not None
        and _hierarchy_separation_usable(record.provider, record.separation_arcsec)
    ):
        end = _offset_position(start[0], start[1], record.separation_arcsec, record.pa_deg)
        geometry_status = "usable"
    elif record.ra_deg is not None and record.dec_deg is not None:
        geometry_status = "base_only"
    relation_type = _wds_graph_relation_type(reference, component, source_component)
    return StructuralEdge(
        source=record.provider,
        source_id=record.source_id,
        record_id=record.id,
        native_id=record.native_id,
        source_component=source_component,
        reference_label=reference,
        component_label=component,
        relation_type=relation_type,
        structural_role=_default_graph_structural_role(relation_type),
        status="derived",
        geometry_status=geometry_status,
        start_ra_deg=None if start is None else start[0],
        start_dec_deg=None if start is None else start[1],
        end_ra_deg=None if end is None else end[0],
        end_dec_deg=None if end is None else end[1],
        separation_arcsec=record.separation_arcsec,
        pa_deg=record.pa_deg,
        relation_epoch=record.measure_epoch,
        note=str(raw_payload.get("Notes") or raw_payload.get("notes") or ""),
    )


def _wds_graph_component_pair(
    record: HierarchyRecord,
    record_index: dict[tuple[int, str, str], HierarchyRecord],
) -> tuple[str | None, str | None]:
    component = (record.component or "").strip()
    if not component:
        if (record.source_id, record.native_id, "AB") not in record_index:
            return "A", "B"
        return None, None
    return _wds_component_pair(component)


def _wds_graph_start_position(
    record: HierarchyRecord,
    record_index: dict[tuple[int, str, str], HierarchyRecord],
    reference: str,
) -> tuple[float, float] | None:
    if record.ra_deg is None or record.dec_deg is None:
        return None
    reference = reference.strip()
    if len(reference) <= 1 or "," in reference:
        return record.ra_deg, record.dec_deg
    group_record = record_index.get((record.source_id, record.native_id, reference))
    if (
        group_record is None
        or group_record.ra_deg is None
        or group_record.dec_deg is None
        or group_record.separation_arcsec is None
        or group_record.pa_deg is None
        or not _hierarchy_separation_usable(group_record.provider, group_record.separation_arcsec)
    ):
        return record.ra_deg, record.dec_deg
    endpoint = _offset_position(
        group_record.ra_deg,
        group_record.dec_deg,
        group_record.separation_arcsec,
        group_record.pa_deg,
    )
    return _midpoint_position(group_record.ra_deg, group_record.dec_deg, endpoint[0], endpoint[1])


def _midpoint_position(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> tuple[float, float]:
    x1, y1, z1 = _unit_vector(ra1_deg, dec1_deg)
    x2, y2, z2 = _unit_vector(ra2_deg, dec2_deg)
    x = x1 + x2
    y = y1 + y2
    z = z1 + z2
    norm = math.sqrt(x * x + y * y + z * z)
    if norm == 0:
        return ra1_deg, dec1_deg
    ra = math.degrees(math.atan2(y / norm, x / norm)) % 360.0
    dec = math.degrees(math.asin(max(-1.0, min(1.0, z / norm))))
    return ra, dec


def _unit_vector(ra_deg: float, dec_deg: float) -> tuple[float, float, float]:
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    cos_dec = math.cos(dec)
    return cos_dec * math.cos(ra), cos_dec * math.sin(ra), math.sin(dec)


def _wds_graph_relation_type(reference: str, component: str, original_component: str | None) -> str:
    if _wds_same_group(reference, component):
        return "internal"
    if _wds_structural_group_reference(reference, component, original_component):
        return "group"
    return "cross_link"


def _default_graph_structural_role(relation_type: str) -> str:
    return "structural" if relation_type in {"group", "internal"} else "non_structural"


def _wds_same_group(left: str, right: str) -> bool:
    return _wds_component_group(left) == _wds_component_group(right)


def _wds_component_group(label: str) -> str:
    text = label.strip()
    if not text:
        return ""
    return text[0].upper()


def _wds_structural_group_reference(reference: str, component: str, original_component: str | None) -> bool:
    reference = reference.strip()
    component = component.strip()
    original = (original_component or "").strip()
    if "," in original:
        return True
    return (
        len(reference) == 1
        and len(component) == 1
        and reference.isalpha()
        and component.isalpha()
        and reference.upper() != component.upper()
    )


def _demote_ambiguous_structural_edges(edges: list[StructuralEdge]) -> None:
    parents_by_child: dict[tuple[int, str, str, str], set[str]] = {}
    for edge in edges:
        if edge.structural_role != "structural" or edge.relation_type != "group":
            continue
        child = _graph_conceptual_component(edge.component_label)
        parent = _graph_conceptual_component(edge.reference_label)
        if not child or not parent or child == parent:
            continue
        parents_by_child.setdefault(
            (edge.source_id, edge.source, edge.native_id, child),
            set(),
        ).add(parent)
    ambiguous_children = {
        key for key, parents in parents_by_child.items()
        if len(parents) > 1
    }
    if not ambiguous_children:
        return
    for edge in edges:
        child = _graph_conceptual_component(edge.component_label)
        key = (edge.source_id, edge.source, edge.native_id, child)
        if key in ambiguous_children and edge.relation_type == "group":
            edge.structural_role = "non_structural"
            edge.note = _join_notes(edge.note, "ambiguous structural parent; demoted to non-structural") or ""


def _latest_graph_overrides(
    session: Session,
    edges: list[StructuralEdge],
) -> dict[int, StructuralEdgeAction]:
    if not edges:
        return {}
    sources = sorted({edge.source for edge in edges})
    native_ids = {edge.native_id for edge in edges if edge.native_id is not None}
    # Actions are rare (manual overrides), so fetch them by source only and match
    # in Python — an edge_id/native_id IN clause over a full provider's edges would
    # blow past SQLite's bound-variable limit.
    rows = tuple(
        row
        for row in session.scalars(
            select(StructuralEdgeAction)
            .where(StructuralEdgeAction.source.in_(sources))
            .order_by(StructuralEdgeAction.created_at, StructuralEdgeAction.id)
        )
        if row.native_id is None or row.native_id in native_ids
    )
    latest: dict[int, StructuralEdgeAction] = {}
    for edge in edges:
        for row in rows:
            if row.edge_id == edge.id or (
                row.source == edge.source
                and row.native_id == edge.native_id
                and row.reference_label == edge.reference_label
                and row.component_label == edge.component_label
            ):
                latest[edge.id] = row
    return latest


def _graph_edges_for_system(
    session: Session,
    *,
    provider: str,
    native_id: str,
    source_id: int,
) -> tuple[HierarchyGraphEdgeRow, ...]:
    """Return graph edge rows for one native hierarchy system.

    This is intentionally session-injected so higher-level context builders can
    avoid opening nested sessions for every candidate system.
    """
    edges = tuple(session.scalars(
        select(StructuralEdge)
        .where(StructuralEdge.source == provider.lower().strip())
        .where(StructuralEdge.native_id == native_id.strip())
        .where(StructuralEdge.source_id == source_id)
        .where(StructuralEdge.status.in_(_GRAPH_EDGE_STATUSES))
        .order_by(
            StructuralEdge.source,
            StructuralEdge.native_id,
            StructuralEdge.reference_label,
            StructuralEdge.component_label,
            StructuralEdge.id,
        )
    ))
    overrides = _latest_graph_overrides(session, list(edges))
    return tuple(_graph_edge_row(edge, overrides.get(edge.id)) for edge in edges)


def _graph_edge_row(edge: StructuralEdge, override: StructuralEdgeAction | None) -> HierarchyGraphEdgeRow:
    relation_type = override.new_relation_type if override is not None and override.new_relation_type else edge.relation_type
    structural_role = (
        override.new_structural_role
        if override is not None and override.new_structural_role
        else edge.structural_role
    )
    status = override.new_status if override is not None and override.new_status else edge.status
    return HierarchyGraphEdgeRow(
        edge_id=edge.id,
        source_id=edge.source_id,
        record_id=edge.record_id,
        provider=edge.source,
        native_id=edge.native_id,
        source_component=edge.source_component,
        reference_label=edge.reference_label,
        component_label=edge.component_label,
        relation_type=relation_type,
        structural_role=structural_role,
        status=status,
        geometry_status=edge.geometry_status,
        start_ra_deg=edge.start_ra_deg,
        start_dec_deg=edge.start_dec_deg,
        end_ra_deg=edge.end_ra_deg,
        end_dec_deg=edge.end_dec_deg,
        separation_arcsec=edge.separation_arcsec,
        pa_deg=edge.pa_deg,
        relation_epoch=edge.relation_epoch,
        note=edge.note,
        override_id=None if override is None else override.id,
        override_actor=None if override is None else override.actor,
        override_reason=None if override is None else override.reason,
    )


def _graph_diagnostic_row(
    provider: str,
    source_id: int,
    native_id: str,
    issue: str,
    severity: str,
    rows: list[HierarchyGraphEdgeRow],
    structural_count: int,
    non_structural_count: int,
    matched_candidate_count: int,
    detail: str,
) -> HierarchyGraphDiagnosticRow:
    return HierarchyGraphDiagnosticRow(
        provider=provider,
        source_id=source_id,
        native_id=native_id,
        issue=issue,
        severity=severity,
        edge_count=len(rows),
        structural_count=structural_count,
        non_structural_count=non_structural_count,
        matched_candidate_count=matched_candidate_count,
        detail=detail,
    )


def _graph_structural_roots(rows: list[HierarchyGraphEdgeRow]) -> set[str]:
    references = {
        _graph_conceptual_component(row.reference_label)
        for row in rows
        if row.reference_label
    }
    components = {
        _graph_conceptual_component(row.component_label)
        for row in rows
        if row.component_label
    }
    return {label for label in references if label not in components}


def _graph_duplicate_parents(rows: list[HierarchyGraphEdgeRow]) -> dict[str, set[str]]:
    parents_by_component: dict[str, set[str]] = {}
    for row in rows:
        if not row.component_label or not row.reference_label:
            continue
        component = _graph_conceptual_component(row.component_label)
        parent = _graph_conceptual_component(row.reference_label)
        if component == parent:
            continue
        parents_by_component.setdefault(component, set()).add(parent)
    return {
        component: parents
        for component, parents in parents_by_component.items()
        if len(parents) > 1
    }


def _graph_conceptual_component(label: str | None) -> str:
    text = (label or "").strip()
    if not text:
        return ""
    if "," in text:
        return ",".join(_graph_conceptual_component(part) for part in text.split(","))
    return text[0].upper()


def _graph_geometry_problem_detail(rows: list[HierarchyGraphEdgeRow]) -> str:
    details: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.source_component:
            label = f"{row.source_component} {row.reference_label}->{row.component_label}"
        else:
            label = f"blank Comp interpreted as {row.reference_label}->{row.component_label}"
        missing = []
        if row.separation_arcsec is None:
            missing.append("rho")
        if row.pa_deg is None:
            missing.append("PA")
        missing_text = f"; missing {', '.join(missing)}" if missing else ""
        detail = f"{label} geometry {row.geometry_status}{missing_text}"
        if detail not in seen:
            seen.add(detail)
            details.append(detail)
        if len(details) == 5:
            break
    return "; ".join(details)


def _parse_ccdm_fixed(line: str) -> ParsedHierarchyRecord | None:
    if not line.strip() or line.lstrip().startswith(("#", ";")):
        return None
    native = line[1:11].strip() if len(line) >= 11 else ""
    component = None
    discoverer = None
    year = None
    pa = None
    separation = None
    vmag = None
    raw_payload: dict[str, object] = {"line": line, "coordinate_source": "ccdm_id_only"}
    if native and ("+" in native or "-" in native):
        rcomp = line[11:12].strip()
        comp = line[12:13].strip()
        component = _ccdm_component_label(rcomp, comp)
        raw_payload.update({
            "rComp": rcomp,
            "Comp": comp,
            "component_label": component,
        })
        discoverer = " ".join(line[15:22].split()) or None
        dra_seconds = _float_text(line[23:30].strip())
        ddec_arcsec = _float_text(line[30:37].strip())
        year = _float_text(line[41:45].strip())
        pa = _float_text(line[46:49].strip())
        separation = _float_text(line[49:55].strip())
        vmag = _float_text(line[59:63].strip())
        pm_note = line[66:67].strip()
        pm_ra = _float_text(line[67:72].strip())
        pm_dec = _float_text(line[72:77].strip())
        raw_payload.update({
            "dRAs": dra_seconds,
            "dDEs": ddec_arcsec,
            "pmNote": pm_note,
            "pmRA_masyr": pm_ra,
            "pmDE_masyr": pm_dec,
        })
    else:
        tokens = line.split()
        native = next((token for token in tokens if token.upper().startswith("J") and len(token) >= 10), None)
        if native is None and tokens and tokens[0].upper() == "CCDM" and len(tokens) > 1:
            native = tokens[1]
        if native is None:
            native = tokens[0] if tokens else None
    if native is None:
        return None
    native = native.replace("CCDM", "").strip()
    ra, dec = _coords_from_hierarchy_id(native)
    if native and ("+" in native or "-" in native):
        ra, dec = _ccdm_precise_position(native, raw_payload)
    if component is None:
        component = _component_from_ccdm_id(native)
    return ParsedHierarchyRecord(
        native_id=native,
        component=component,
        discoverer_id=discoverer,
        ra_deg=ra,
        dec_deg=dec,
        last_epoch=year,
        measure_epoch=year,
        separation_arcsec=separation,
        pa_deg=pa,
        magnitude_primary=vmag,
        raw_payload=raw_payload,
    )


def _ccdm_component_label(reference: str, component: str) -> str | None:
    if not reference and not component:
        return None
    return f"{reference}{component}".strip() or None


def _ccdm_precise_position(
    native: str,
    raw_payload: dict[str, object],
) -> tuple[float | None, float | None]:
    base_ra, base_dec = _coords_from_hierarchy_id(native)
    if base_ra is None or base_dec is None:
        return base_ra, base_dec
    dra_seconds = raw_payload.get("dRAs")
    ddec_arcsec = raw_payload.get("dDEs")
    if dra_seconds is None and ddec_arcsec is None:
        raw_payload["coordinate_source"] = "ccdm_id_only"
        return base_ra, base_dec
    ra = base_ra
    dec = base_dec
    if isinstance(dra_seconds, int | float) and math.isfinite(float(dra_seconds)):
        # CCDM dRAs is a remainder in seconds of time relative to the truncated
        # catalog identifier coordinate.
        ra = (ra + float(dra_seconds) * 15.0 / 3600.0) % 360.0
    if isinstance(ddec_arcsec, int | float) and math.isfinite(float(ddec_arcsec)):
        dec = dec + float(ddec_arcsec) / 3600.0
    raw_payload["coordinate_source"] = "ccdm_remainder"
    return ra, dec


def _coords_from_hierarchy_id(value: str) -> tuple[float | None, float | None]:
    text = value.strip()
    if text.upper().startswith("J"):
        text = text[1:]
    sign_index = max(text.find("+"), text.find("-"))
    if sign_index < 0:
        return None, None
    ra_text = text[:sign_index]
    dec_text = text[sign_index:]
    sign = -1.0 if dec_text.startswith("-") else 1.0
    dec_body = dec_text[1:]
    try:
        if len(ra_text) >= 5:
            hours = int(ra_text[0:2])
            minutes = float(ra_text[2:]) / (10 ** max(0, len(ra_text[2:]) - 2))
        else:
            return None, None
        if len(dec_body) >= 4:
            degrees = int(dec_body[0:2])
            arcmin = float(dec_body[2:4])
        else:
            return None, None
    except ValueError:
        return None, None
    ra_deg = (hours + minutes / 60.0) * 15.0
    dec_deg = sign * (degrees + arcmin / 60.0)
    if not math.isfinite(ra_deg) or not math.isfinite(dec_deg):
        return None, None
    return ra_deg % 360.0, dec_deg


def _component_from_ccdm_id(value: str) -> str | None:
    tail = ""
    for character in reversed(value.strip()):
        if character.isalpha():
            tail = character + tail
        else:
            break
    return tail or None


def _first_text(row: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key.lower())
        if value:
            return value
    return None


def _first_float(row: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        value = _float_text(row.get(key.lower(), ""))
        if value is not None:
            return value
    return None


def _first_ra_deg(row: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        text = row.get(key.lower(), "")
        value = _float_text(text)
        if value is not None:
            return value
        value = _ra_text_to_deg(text)
        if value is not None:
            return value
    return None


def _first_dec_deg(row: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        text = row.get(key.lower(), "")
        value = _float_text(text)
        if value is not None:
            return value
        value = _dec_text_to_deg(text)
        if value is not None:
            return value
    return None


def _ra_text_to_deg(value: object) -> float | None:
    parts = _sexagesimal_parts(value)
    if parts is None:
        return None
    hours, minutes, seconds = parts
    ra_deg = (abs(hours) + minutes / 60.0 + seconds / 3600.0) * 15.0
    return ra_deg % 360.0 if math.isfinite(ra_deg) else None


def _dec_text_to_deg(value: object) -> float | None:
    parts = _sexagesimal_parts(value)
    if parts is None:
        return None
    degrees, minutes, seconds = parts
    sign = -1.0 if str(value).strip().startswith("-") or degrees < 0 else 1.0
    dec_deg = sign * (abs(degrees) + minutes / 60.0 + seconds / 3600.0)
    return dec_deg if math.isfinite(dec_deg) else None


def _sexagesimal_parts(value: object) -> tuple[float, float, float] | None:
    text = str(value).strip()
    if not text or ":" not in text and " " not in text:
        return None
    tokens = [token for token in re.split(r"[:\s]+", text) if token]
    if len(tokens) < 3:
        return None
    try:
        return float(tokens[0]), abs(float(tokens[1])), abs(float(tokens[2]))
    except ValueError:
        return None


def _float_token(tokens: list[str], index: int) -> float | None:
    if index < 0 or index >= len(tokens):
        return None
    return _float_text(tokens[index])


def _float_text(value: object) -> float | None:
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or text in {".", "-", "--"}:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _delta_mag(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    return round(second - first, 6)
