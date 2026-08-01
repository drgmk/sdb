from __future__ import annotations

import json
import math
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .adapters.review_metadata import normalize_review_payload
from .astrometry import angular_separation_arcsec
from .hierarchy_identity_context import (
    target_semantic_identity,
    target_semantic_identity_summary,
)
from .hierarchy_photometry import target_photometry_context_summary
from .hierarchy_semantics import component_label_from_identifier
from .hierarchy_target_context import (
    HierarchyTargetContextService,
    candidate_projection,
)
from .identifiers import normalize_identifier
from .models import (
    AstrometricSolution,
    CatalogRun,
    ExternalIdentifier,
    HierarchyMatchCandidate,
    HierarchyRecord,
    MatchCandidate,
    MetadataRun,
    RawCatalogRow,
    SimbadMetadata,
    Submission,
    Target,
    TargetSystemMember,
)
from .providers import Astrometry
from .system_photometry import SystemPhotometryState, load_system_photometry_state
from .targets import resolve_target
from .vocabulary import ProviderRunStatus


class HierarchySystemContextService:
    """Build the read-only, system-level hierarchy review projection."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def system_context(
        self,
        target_reference: str | int,
        *,
        catalog_providers: Iterable[str] | None = None,
        radius_arcsec: float | None = None,
    ) -> dict[str, object]:
        if radius_arcsec is not None:
            radius_arcsec = float(radius_arcsec)
            if not math.isfinite(radius_arcsec) or not 1.0 <= radius_arcsec <= 600.0:
                raise ValueError("system context radius must be between 1 and 600 arcsec")

        target_context = HierarchyTargetContextService(
            self.session_factory,
        ).target_context(target_reference, include_diagnostics=True)
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
                for member in session.scalars(
                    select(Target).where(
                        Target.id.in_(explicit_target_ids - present_target_ids)
                    )
                )
            )
            nearby_targets.sort(key=lambda row: float(row["separation_arcsec"]))
            target_ids = sorted(
                {
                    target.id,
                    *explicit_target_ids,
                    *(int(row["target_id"]) for row in nearby_targets),
                }
            )
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
                session,
                target_ids,
                expand_context=False,
            )
            photometry = _system_photometry(photometry_state)
            measurement_assignments = _system_measurement_assignments(photometry_state)
            target_lifecycle = _system_target_lifecycle(photometry_state)
            system_memberships = _system_memberships(photometry_state)
            simbad_metadata = _system_simbad_metadata(session, target_ids)
            simbad_main_ids = _system_simbad_main_ids(
                session,
                target_ids,
                metadata_by_target=simbad_metadata,
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
            semantic = {}
            for row in nearby_targets:
                nearby_target = session.get(Target, int(row["target_id"]))
                if nearby_target is not None:
                    semantic[str(row["sdbid"])] = target_semantic_identity_summary(
                        target_semantic_identity(session, nearby_target)
                    )
            result = {
                "target": target_context["target"],
                "radius_arcsec": radius_arcsec,
                "target_context": {
                    "classification": target_context["classification"],
                    "hierarchy_decision_basis": target_context[
                        "hierarchy_decision_basis"
                    ],
                    "component_assignment": target_context["component_assignment"],
                    "photometry_context": target_photometry_context_summary(
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
                self.session_factory,
                target_reference,
            )
        except ValueError as error:
            result["simbad_relative_preview"] = []
            result["notes"].append(str(error))
        return result


def _target_context_system_keys(
    context: dict[str, object],
) -> set[tuple[str, int, str]]:
    keys = set()
    for system in context.get("systems") or []:
        source_id = system.get("source_id")
        if source_id is None:
            continue
        keys.add((str(system["provider"]), int(source_id), str(system["native_id"])))
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
            if existing is None or float(value["separation_from_target_arcsec"]) < float(
                existing["separation_from_target_arcsec"]
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
    maximum = max(
        float(row["separation_from_target_arcsec"]) for row in component_positions
    )
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
        if separation <= radius_arcsec:
            rows.append(_system_target_review_row(session, target, other))
    return sorted(rows, key=lambda row: float(row["separation_arcsec"]))


def _explicit_system_target_ids(session: Session, target_id: int) -> set[int]:
    system_ids = set(
        session.scalars(
            select(TargetSystemMember.system_id).where(
                TargetSystemMember.target_id == target_id
            )
        )
    )
    if not system_ids:
        return {target_id}
    return set(
        session.scalars(
            select(TargetSystemMember.target_id).where(
                TargetSystemMember.system_id.in_(system_ids)
            )
        )
    ) | {target_id}


def _system_target_review_row(
    session: Session,
    requested_target: Target,
    target: Target,
) -> dict[str, object]:
    separation = angular_separation_arcsec(
        Astrometry(requested_target.ra2000_deg, requested_target.dec2000_deg),
        Astrometry(target.ra2000_deg, target.dec2000_deg),
    )
    identifiers = list(
        session.scalars(
            select(ExternalIdentifier.value)
            .where(ExternalIdentifier.target_id == target.id)
            .order_by(ExternalIdentifier.source, ExternalIdentifier.value)
            .limit(12)
        )
    )
    canonical = (
        None
        if target.canonical_astrometry_id is None
        else session.get(AstrometricSolution, target.canonical_astrometry_id)
    )
    return {
        "target_id": target.id,
        "sdbid": target.sdbid,
        "ra2000_deg": target.ra2000_deg,
        "dec2000_deg": target.dec2000_deg,
        "separation_arcsec": separation,
        "is_requested_target": target.id == requested_target.id,
        "canonical_astrometry": None
        if canonical is None
        else {
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
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
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
        distance_pc = None if parallax is None or parallax <= 0 else 1000.0 / parallax
        distance_error_pc = (
            None
            if distance_pc is None or parallax_error is None or parallax_error < 0
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
    result = {
        sdbid: str(metadata["main_id"])
        for sdbid, metadata in metadata_by_target.items()
        if metadata.get("main_id")
    }
    if not target_ids:
        return result
    targets = {
        target.id: target.sdbid
        for target in session.scalars(select(Target).where(Target.id.in_(target_ids)))
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
    result: dict[str, list[dict[str, object]]] = {
        sdbid: [] for sdbid in targets.values()
    }
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
        if (record.provider, record.source_id, record.native_id) not in system_keys:
            continue
        result[targets[candidate.target_id]].append(
            candidate_projection(candidate, record)
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
                identifier_matches.setdefault(key, []).append(
                    {
                        "target_id": target["target_id"],
                        "sdbid": sdbid,
                        "candidate_id": candidate["candidate_id"],
                        "match_method": match_method,
                        "separation_arcsec": candidate.get("separation_arcsec"),
                    }
                )

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
                row["separation_arcsec"]
                if row["separation_arcsec"] is not None
                else math.inf,
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
        match_separation = (
            None
            if sky_match is None
            else sky_match["component_match_separation_arcsec"]
        )
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

        annotated.append(
            {
                **component,
                "linked_sdbid": chosen_sdbid,
                "linked_target_id": chosen_target_id,
                "component_target_role": role,
                "component_match_basis": match_basis,
                "component_match_separation_arcsec": match_separation,
                "component_match_conflict": conflict,
                "identifier_match_sdbids": list(id_sdbids),
                "sky_match_sdbid": None if sky_match is None else sky_match["sdbid"],
                "sky_match_separation_arcsec": (
                    None
                    if sky_match is None
                    else sky_match["component_match_separation_arcsec"]
                ),
                "position_match_threshold_arcsec": position_threshold_arcsec,
            }
        )
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
            candidates.append(
                {**target, "component_match_separation_arcsec": separation}
            )
    return min(
        candidates,
        key=lambda row: (
            float(row["component_match_separation_arcsec"]),
            str(row["sdbid"]),
        ),
        default=None,
    )


def _system_photometry(
    state: SystemPhotometryState,
) -> dict[str, list[dict[str, object]]]:
    if not state.selected_target_ids:
        return {}
    targets = {target.id: target.sdbid for target in state.targets.values()}
    result: dict[str, list[dict[str, object]]] = {
        sdbid: [] for sdbid in targets.values()
    }
    for encounter in state.encounters:
        measurement = encounter.measurement
        result[targets[encounter.target_id]].append(
            {
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
            }
        )
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
            else state.referenced_targets.get(lifecycle.superseded_by_target_id)
        )
        result[target.sdbid] = {
            "target_id": target.id,
            "role": lifecycle.role.value,
            "state": lifecycle.state.value,
            "superseded_by_sdbid": None
            if replacement is None
            else replacement.sdbid,
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
        result[target.sdbid] = [
            {
                "system_id": membership.system_id,
                "system_name": membership.name,
                "component_label": membership.component_label,
                "source": membership.source,
                "is_primary": membership.primary,
            }
            for membership in memberships
        ]
    return dict(sorted(result.items()))


def _system_measurement_assignments(
    state: SystemPhotometryState,
) -> list[dict[str, object]]:
    if not state.selected_target_ids:
        return []
    measurements = list(state.measurements.values())
    measurements.sort(
        key=lambda value: (value.provider, value.source_id, value.band, value.id)
    )
    if not measurements:
        return []
    associations_by_measurement = {}
    for association in state.assignments:
        associations_by_measurement.setdefault(association.measurement_id, []).append(
            association
        )
    targets = {
        target.id: target.sdbid for target in state.referenced_targets.values()
    }
    return [
        {
            "measurement_id": measurement.id,
            "origin_target_id": measurement.target_id,
            "origin_sdbid": targets.get(measurement.target_id),
            "provider": measurement.provider,
            "source_id": measurement.source_id,
            "band": measurement.band,
            "value": measurement.value,
            "unit": measurement.unit,
            "contributors": [
                {
                    "association_id": association.association_id,
                    "target_id": association.target_id,
                    "sdbid": targets.get(association.target_id),
                    "role": association.role,
                    "method": association.method,
                    "weight": association.weight,
                    "note": association.note,
                    "derived": association.derived,
                }
                for association in associations_by_measurement.get(measurement.id, [])
            ],
        }
        for measurement in measurements
    ]


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
    result: dict[str, list[dict[str, object]]] = {
        sdbid: [] for sdbid in targets.values()
    }

    from .catalog_results import (
        effective_catalog_results,
        effective_catalog_selected_rows,
    )

    effective = effective_catalog_results(session, target_ids)
    selected_raw_ids = {
        raw.id
        for current in effective.values()
        for raw, _detection in effective_catalog_selected_rows(session, current)
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
        result[targets[run.target_id]].append(
            {
                "provider": run.provider,
                "run_id": run.id,
                "raw_row_id": row.id,
                "source_id": row.source_id,
                "accepted": current is not None and row.id in selected_raw_ids,
                "run_status": run.status if current is None else current.status.value,
                "separation_arcsec": row.separation_arcsec,
                "score": row.score,
                "ra_deg": row.ra_deg,
                "dec_deg": row.dec_deg,
                "epoch": row.epoch,
                "neighbourhood_flags": _catalog_neighbourhood_flags(
                    run.provider,
                    payload,
                ),
            }
        )
    return {key: value for key, value in result.items() if value}


def _catalog_neighbourhood_flags(
    provider: str,
    payload: dict[str, object],
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

    selected_ids = effective_identity_candidate_ids(session, target_ids=[target.id])
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
        if matched_targets:
            rows.append(
                {
                    "candidate_id": candidate.id,
                    "provider": candidate.provider,
                    "source_id": candidate.source_id,
                    "accepted": candidate.id in selected_ids,
                    "separation_arcsec": candidate.separation_arcsec,
                    "score": candidate.score,
                    "matched_nearby_targets": matched_targets,
                }
            )
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
        index.setdefault(identifier.normalized_value, []).append(
            {
                "target_id": identifier.target_id,
                "sdbid": targets[identifier.target_id],
                "match_source": "external_identifier",
                "identifier": identifier.value,
            }
        )
    for solution in session.scalars(
        select(AstrometricSolution)
        .where(AstrometricSolution.target_id.in_(target_ids))
        .where(AstrometricSolution.source_id.is_not(None))
        .order_by(AstrometricSolution.target_id, AstrometricSolution.id)
    ):
        values = {str(solution.source_id), f"{solution.source} {solution.source_id}"}
        if solution.source == "gaia_dr3":
            values.add(f"Gaia DR3 {solution.source_id}")
        for value in values:
            index.setdefault(normalize_identifier(value), []).append(
                {
                    "target_id": solution.target_id,
                    "sdbid": targets[solution.target_id],
                    "match_source": "astrometric_solution",
                    "identifier": value,
                }
            )
    return index


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
