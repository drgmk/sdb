from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import Integer, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import angular_separation_arcsec
from .adapters.review_metadata import normalize_review_payload
from .catalog_measurements import (
    current_measurements_for_target,
)
from .dirty import mark_export_dirty
from .decisions import DecisionContext
from .models import (
    AstrometricSolution,
    ExternalIdentifier,
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
    TargetSystem,
    TargetSystemMember,
)
from .system_photometry import (
    SystemPhotometryState,
    load_system_photometry_state,
)
from .providers import Astrometry
from .identifiers import normalize_identifier
from .hierarchy_semantics import (
    component_label_from_identifier,
    normalize_component_label,
    simbad_component_relevance,
)
from .hierarchy_graph import (
    GRAPH_EDGE_STATUSES as _GRAPH_EDGE_STATUSES,
    HierarchyGraphDeriveResult,
    HierarchyGraphDiagnosticRow,
    HierarchyGraphEdgeRow,
    HierarchyGraphOverrideResult,
    build_wds_record_index as _build_wds_record_index,
    build_diagnostics as _build_graph_diagnostics,
    copy_edge_values as _copy_graph_edge_values,
    demote_ambiguous_edges as _demote_ambiguous_structural_edges,
    derive_wds_edge as _wds_graph_edge_for_record,
    edge_key as _graph_edge_key,
    edge_row as _graph_edge_row,
    edges_for_system as _graph_edges_for_system,
    latest_overrides as _latest_graph_overrides,
)
from .hierarchy_matching import (
    HierarchyMatchActionResult,
    HierarchyMatchResult,
    HierarchyMatchReviewRow,
    HierarchyMatchingService,
    HierarchyTargetMatchResult,
)
from .hierarchy_sources import (
    HierarchyImportResult,
    HierarchyPruneResult,
    HierarchySourceService,
)
from .targets import resolve_target
from .snapshots import SnapshotClient
from .vocabulary import ProviderRunStatus, ReviewPriority, review_priority_rank


# Structural edges hold both provider-derived graph edges and target-resolved
# relationships in one table. Graph readers see only re-derivable provider edges;
# relationship readers see only accepted assertions.
_RELATIONSHIP_STATUS = "accepted"


def _relationship_status(status: str) -> str:
    """Normalize a relationship status, mapping the legacy default to accepted."""
    clean = status.strip()
    return _RELATIONSHIP_STATUS if clean in ("", "current") else clean



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

    def system_context(
        self,
        target_reference: str | int,
        *,
        catalog_providers: Iterable[str] | None = None,
        radius_arcsec: float | None = None,
    ) -> dict[str, object]:
        """Return a read-only, system-level review context for one target.

        This deliberately composes existing target-level evidence rather than
        persisting a new system interpretation. It is intended to make cases
        such as A/B components, rejected sibling Gaia candidates, and blended
        photometry visible before we decide what should become auditable state.
        """
        if radius_arcsec is not None:
            radius_arcsec = float(radius_arcsec)
            if (
                not math.isfinite(radius_arcsec)
                or not 1.0 <= radius_arcsec <= 600.0
            ):
                raise ValueError("system context radius must be between 1 and 600 arcsec")
        target_context = self.target_context(target_reference, include_diagnostics=True)
        with self.session_factory() as session:
            target = _find_required_target(session, target_reference)
            system_keys = _target_context_system_keys(target_context)
            component_positions = _system_component_positions(target_context)
            if radius_arcsec is None:
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
            photometry_state = load_system_photometry_state(
                session, target_ids, expand_context=False,
            )
            photometry = _system_photometry(photometry_state)
            measurement_assignments = _system_measurement_assignments(
                photometry_state
            )
            target_lifecycle = _system_target_lifecycle(photometry_state)
            system_memberships = _system_memberships(photometry_state)
            simbad_metadata = _system_simbad_metadata(session, target_ids)
            simbad_main_ids = _system_simbad_main_ids(
                session, target_ids, metadata_by_target=simbad_metadata,
            )
            catalog_neighbourhood = _system_catalog_neighbourhood(session, target_ids)
            from .catalog_associations import (
                catalog_coverage_by_target,
                catalog_target_candidates,
            )

            catalog_candidates = catalog_target_candidates(session, target_ids)
            catalog_coverage = catalog_coverage_by_target(
                session,
                sorted({target.id, *explicit_target_ids}),
                providers=catalog_providers,
            )
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
                "catalog_target_candidates": catalog_candidates,
                "catalog_coverage_by_target": catalog_coverage,
                "notes": [
                    "read-only review context; no system/export decisions are persisted",
                    "identity_cross_candidates show rejected/accepted source candidates that resolve to another nearby SDB target",
                    "catalog_target_candidates re-evaluate current provider detections against every target in the review neighbourhood",
                    "catalog_coverage_by_target reports direct provider-query coverage for explicit system members",
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
        min_rank = 0 if min_priority is None else review_priority_rank(min_priority)
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
            if review_priority_rank(str(row["priority"])) < min_rank:
                continue
            rows.append(row)
        return sorted(
            rows,
            key=lambda row: (
                -review_priority_rank(str(row["priority"])),
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
        return HierarchySourceService(self.session_factory).import_snapshot(
            provider, path, release=release, note=note,
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
        return HierarchySourceService(self.session_factory).fetch_snapshot(
            provider,
            client=client,
            cache_path=cache_path,
            refresh_cache=refresh_cache,
            release=release,
            note=note,
        )

    def sources(self, provider: str | None = None) -> tuple[HierarchySource, ...]:
        return HierarchySourceService(self.session_factory).sources(provider)

    def prune_duplicate_sources(
        self, provider: str | None = None,
    ) -> HierarchyPruneResult:
        return HierarchySourceService(
            self.session_factory,
        ).prune_duplicate_sources(provider)

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
        return HierarchyMatchingService(self.session_factory).match_records(
            provider, source_id=source_id, radius_arcsec=radius_arcsec,
        )

    def match_targets(
        self,
        provider: str,
        target_references: Iterable[str | int],
        *,
        radius_arcsec: float = 30.0,
    ) -> HierarchyTargetMatchResult:
        return HierarchyMatchingService(self.session_factory).match_targets(
            provider, target_references, radius_arcsec=radius_arcsec,
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
        return _build_graph_diagnostics(
            rows,
            candidate_counts,
            limit=limit,
            severity=severity_value,
            issue=issue_value,
        )

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

    def review_matches(
        self, provider: str | None = None,
    ) -> tuple[HierarchyMatchReviewRow, ...]:
        return HierarchyMatchingService(self.session_factory).review_matches(provider)

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
        return HierarchyMatchingService(self.session_factory).accept_match(
            candidate_id,
            actor=actor,
            reason=reason,
            system=system,
            component_label=component_label,
            relationship_type=relationship_type,
        )

    def reject_match(
        self,
        candidate_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> HierarchyMatchActionResult:
        return HierarchyMatchingService(self.session_factory).reject_match(
            candidate_id, actor=actor, reason=reason,
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
            MetadataRun.status == ProviderRunStatus.MATCH,
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
        label = component_label_from_identifier(str(identifier))
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
    state: SystemPhotometryState,
) -> dict[str, list[dict[str, object]]]:
    if not state.selected_target_ids:
        return {}
    targets = {
        target.id: target.sdbid for target in state.targets.values()
    }
    result: dict[str, list[dict[str, object]]] = {sdbid: [] for sdbid in targets.values()}
    for encounter in state.encounters:
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
    state: SystemPhotometryState,
) -> dict[str, dict[str, object]]:
    result = {}
    for target_id, target in state.targets.items():
        lifecycle = state.lifecycle[target_id]
        replacement = (
            None
            if lifecycle.superseded_by_target_id is None
            else state.referenced_targets.get(
                lifecycle.superseded_by_target_id
            )
        )
        result[target.sdbid] = {
            "target_id": target.id,
            "role": lifecycle.role.value,
            "state": lifecycle.state.value,
            "superseded_by_sdbid": None if replacement is None else replacement.sdbid,
            "action_id": lifecycle.action_id,
        }
    return dict(sorted(result.items()))


def _system_memberships(
    state: SystemPhotometryState,
) -> dict[str, list[dict[str, object]]]:
    if not state.selected_target_ids:
        return {}
    result: dict[str, list[dict[str, object]]] = {}
    for target_id, memberships in state.system_memberships.items():
        target = state.targets.get(target_id)
        if target is None:
            continue
        result[target.sdbid] = [{
            "system_id": membership.system_id,
            "system_name": membership.name,
            "component_label": membership.component_label,
            "source": membership.source,
            "is_primary": membership.primary,
        } for membership in memberships]
    return dict(sorted(result.items()))


def _system_measurement_assignments(
    state: SystemPhotometryState,
) -> list[dict[str, object]]:
    if not state.selected_target_ids:
        return []
    measurements = list(state.measurements.values())
    measurements.sort(key=lambda value: (
        value.provider, value.source_id, value.band, value.id,
    ))
    if not measurements:
        return []
    associations_by_measurement = {}
    for association in state.assignments:
        associations_by_measurement.setdefault(association.measurement_id, []).append(association)
    targets = {
        target.id: target.sdbid
        for target in state.referenced_targets.values()
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
            "association_id": association.association_id,
            "target_id": association.target_id,
            "sdbid": targets.get(association.target_id),
            "role": association.role,
            "method": association.method,
            "weight": association.weight,
            "note": association.note,
            "derived": association.derived,
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
    from .catalog_results import (
        effective_catalog_results,
        effective_catalog_selected_rows,
    )

    effective = effective_catalog_results(session, target_ids)
    selected_raw_ids = {
        raw.id
        for current in effective.values()
        for raw, _detection in effective_catalog_selected_rows(
            session, current,
        )
    }
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
        current = effective.get((run.target_id, run.provider))
        payload = _json_payload(row.payload_json)
        result[targets[run.target_id]].append({
            "provider": run.provider,
            "run_id": run.id,
            "raw_row_id": row.id,
            "source_id": row.source_id,
            "accepted": (
                current is not None and row.id in selected_raw_ids
            ),
            "run_status": (
                run.status if current is None else current.status.value
            ),
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
    from .identity_results import effective_identity_candidate_ids

    selected_ids = effective_identity_candidate_ids(
        session, target_ids=[target.id],
    )
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
            "accepted": candidate.id in selected_ids,
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
        label = component_label_from_identifier(value)
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


def _best_component_label_candidate(candidates: list[dict[str, object]]) -> str | None:
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.get("source") == "main_id":
            return str(candidate["label"])
    return str(candidates[0]["label"])


def _component_labels_match(first: str, second: str) -> bool:
    return normalize_component_label(first) == normalize_component_label(second)


def _component_label_is_group(value: str) -> bool:
    label = normalize_component_label(value.strip())
    if not label:
        return False
    if "," in label:
        return True
    return len(label) > 1 and label.isalpha() and label.isupper()


def _component_label_contains(group: str, component: str) -> bool:
    group = normalize_component_label(group)
    component = normalize_component_label(component)
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
        priority = ReviewPriority.HIGHEST
        reason = "likely blended photometry depends on unaccepted hierarchy candidates"
    elif accepted_count == 0 and candidate_system_count > 1:
        priority = ReviewPriority.HIGH
        reason = "multiple candidate hierarchy systems need a decision"
    elif assignment_status == "semantic_geometry_conflict":
        priority = ReviewPriority.HIGH
        reason = "SIMBAD semantic component and provider geometry disagree"
    elif review_required or diagnostic_count:
        priority = ReviewPriority.MEDIUM
        reason = "hierarchy diagnostics or photometry context require review"
    elif candidate_count and accepted_count == 0:
        priority = ReviewPriority.LOW
        reason = "single clean hierarchy candidate has not been accepted"
    elif accepted_count:
        priority = ReviewPriority.LOW
        reason = "accepted hierarchy decision present"
    else:
        priority = ReviewPriority.NONE
        reason = "no hierarchy review item"

    sdbid = str(context["target"]["sdbid"])
    return {
        "sdbid": sdbid,
        "priority": priority.value,
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
    relevance = simbad_component_relevance(value.related_object_type, object_types)
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
    target = resolve_target(session, reference)
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


def _join_notes(*values: str | None) -> str | None:
    notes = [value.strip() for value in values if value and value.strip()]
    return "; ".join(notes) if notes else None
