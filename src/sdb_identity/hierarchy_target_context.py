"""Target-level hierarchy context and review projections."""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import angular_separation_arcsec
from .catalog_measurements import current_measurements_for_target
from .hierarchy_graph import (
    HierarchyGraphEdgeRow,
    diagnostics_for_system,
    edges_for_system,
)
from .hierarchy_identity_context import (
    target_semantic_identity,
    target_semantic_identity_summary,
)
from .hierarchy_photometry import (
    refresh_photometry_band_summaries,
    review_queue_row,
    target_component_assignment,
    target_component_assignment_summary,
    target_photometry_context,
    target_photometry_context_summary,
)
from .models import HierarchyMatchCandidate, HierarchyRecord, Target
from .providers import Astrometry
from .targets import resolve_target
from .vocabulary import review_priority_rank


class HierarchyTargetContextService:
    """Assemble read-only hierarchy context for one target or review queue."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def target_context(
        self,
        target_reference: str | int,
        *,
        include_diagnostics: bool = True,
    ) -> dict[str, object]:
        with self.session_factory() as session:
            target = _required_target(session, target_reference)
            semantic_identity = target_semantic_identity(session, target)
            candidate_rows, decision_basis = _effective_candidate_rows(
                session, target.id,
            )
            keys = sorted({
                (record.provider, record.source_id, record.native_id)
                for _candidate, record in candidate_rows
            })
            target_position = Astrometry(target.ra2000_deg, target.dec2000_deg)
            systems = []
            all_components = []
            review_required = False
            for provider, source_id, native_id in keys:
                edges = []
                diagnostics = []
                if provider == "wds":
                    edges = list(edges_for_system(
                        session,
                        provider=provider,
                        native_id=native_id,
                        source_id=source_id,
                    ))
                    if include_diagnostics:
                        diagnostics = list(diagnostics_for_system(
                            session,
                            provider=provider,
                            native_id=native_id,
                            source_id=source_id,
                        ))
                review_required = review_required or any(
                    row.severity == "review" for row in diagnostics
                )
                components = _context_components(target_position, edges)
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
                    "candidates": [
                        candidate_projection(candidate, record)
                        for candidate, record in candidate_rows
                        if (
                            record.provider == provider
                            and record.source_id == source_id
                            and record.native_id == native_id
                        )
                    ],
                    "components": components,
                    "edges": [asdict(edge) for edge in edges],
                    "diagnostics": [asdict(row) for row in diagnostics],
                })

            nearest, closest_companion = _nearest_components(all_components)
            classification = _classification(
                systems=systems,
                nearest_component=nearest,
                review_required=review_required,
            )
            component_assignment = target_component_assignment(
                semantic_identity=semantic_identity,
                nearest_component=nearest,
                closest_companion=closest_companion,
                systems=systems,
                review_required=review_required,
            )
            photometry_context = target_photometry_context(
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

    def target_context_summary(
        self, target_reference: str | int,
    ) -> dict[str, object]:
        context = self.target_context(target_reference)
        warnings = []
        closest = context["closest_companion"]
        if closest is not None:
            warnings.append(
                "known hierarchy component may affect low-resolution photometry",
            )
        if context["review_required"]:
            warnings.append("hierarchy review diagnostics are present")
        return {
            "classification": context["classification"],
            "review_required": context["review_required"],
            "semantic_identity": target_semantic_identity_summary(
                context["semantic_identity"],
            ),
            "component_assignment": target_component_assignment_summary(
                context["component_assignment"],
            ),
            "photometry_context": target_photometry_context_summary(
                context["photometry_context"],
            ),
            "matched_systems": context["matched_systems"],
            "hierarchy_decision_basis": context["hierarchy_decision_basis"],
            "nearest_component": context["nearest_component"],
            "nearby_components": sum(
                len(system["components"]) for system in context["systems"]
            ),
            "closest_companion": closest,
            "warnings": warnings,
        }

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
                target_references, provider=provider,
            )
        rows = []
        for reference in target_references:
            context = self.target_context(reference, include_diagnostics=False)
            photometry = _filtered_photometry(
                context["photometry_context"], provider,
            )
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
                "likely_unresolved_components": photometry[
                    "likely_unresolved_components"
                ],
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
            photometry = _filtered_photometry(
                context["photometry_context"], provider_value,
            )
            row = review_queue_row(context, photometry)
            if review_priority_rank(str(row["priority"])) >= min_rank:
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
                target = _required_target(session, reference)
                measurements = current_measurements_for_target(session, target.id)
                if any(not provider or value.provider == provider for value in measurements):
                    filtered.append(reference)
        return filtered


def _effective_candidate_rows(
    session: Session, target_id: int,
) -> tuple[tuple[tuple[HierarchyMatchCandidate, HierarchyRecord], ...], str]:
    rows = tuple(session.execute(
        select(HierarchyMatchCandidate, HierarchyRecord)
        .join(
            HierarchyRecord,
            HierarchyRecord.id == HierarchyMatchCandidate.record_id,
        )
        .where(HierarchyMatchCandidate.target_id == target_id)
        .order_by(
            HierarchyRecord.provider,
            HierarchyRecord.source_id,
            HierarchyRecord.native_id,
            HierarchyMatchCandidate.score.desc(),
            HierarchyMatchCandidate.id,
        ),
    ))
    accepted = tuple(
        (candidate, record)
        for candidate, record in rows
        if candidate.status == "accepted"
    )
    if accepted:
        return accepted, "accepted_candidates"
    candidates = tuple(
        (candidate, record)
        for candidate, record in rows
        if candidate.status == "candidate"
    )
    return candidates, "candidate_review" if candidates else "none"


def candidate_projection(
    candidate: HierarchyMatchCandidate, record: HierarchyRecord,
) -> dict[str, object]:
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


def _context_components(
    target_position: Astrometry,
    edges: list[HierarchyGraphEdgeRow],
) -> list[dict[str, object]]:
    components: dict[str, dict[str, object]] = {}
    for edge in edges:
        if edge.status == "rejected":
            continue
        if (
            edge.reference_label
            and edge.start_ra_deg is not None
            and edge.start_dec_deg is not None
        ):
            _add_component(
                components,
                target_position,
                edge.reference_label,
                edge.start_ra_deg,
                edge.start_dec_deg,
                edge,
                "reference",
            )
        if (
            edge.component_label
            and edge.end_ra_deg is not None
            and edge.end_dec_deg is not None
        ):
            _add_component(
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
        key=lambda item: (item["separation_arcsec"], str(item["component"])),
    )


def _add_component(
    components: dict[str, dict[str, object]],
    target_position: Astrometry,
    component: str,
    ra_deg: float,
    dec_deg: float,
    edge: HierarchyGraphEdgeRow,
    role: str,
) -> None:
    separation = angular_separation_arcsec(
        target_position, Astrometry(ra_deg, dec_deg),
    )
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


def _nearest_components(
    all_components: list[dict[str, object]],
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    nearest = min(
        all_components,
        key=lambda component: component["separation_arcsec"],
        default=None,
    )
    if nearest is None:
        return None, None
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
        component["separation_from_nearest_arcsec"] = angular_separation_arcsec(
            Astrometry(nearest["ra_deg"], nearest["dec_deg"]),
            Astrometry(component["ra_deg"], component["dec_deg"]),
        )
    return nearest, min(
        companions,
        key=lambda component: component["separation_from_nearest_arcsec"],
        default=None,
    )


def _classification(
    *,
    systems: list[dict[str, object]],
    nearest_component: dict[str, object] | None,
    review_required: bool,
) -> str:
    if review_required:
        return "review_required"
    if not systems:
        return "single_or_no_known_hierarchy"
    if nearest_component is None:
        return "known_hierarchy_without_component_geometry"
    return "component_of_known_system"


def _filtered_photometry(
    source: dict[str, object], provider: str | None,
) -> dict[str, object]:
    photometry = dict(source)
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
        refresh_photometry_band_summaries(photometry)
    return photometry


def _required_target(session: Session, reference: str | int) -> Target:
    target = resolve_target(session, reference)
    if target is None:
        raise KeyError(f"target not found: {reference}")
    return target
