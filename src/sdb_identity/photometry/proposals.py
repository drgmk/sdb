from __future__ import annotations

import json
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..astrometry import angular_separation_arcsec
from ..catalogs.policy import (
    catalog_source_display_name,
    catalog_source_id_matches_identifiers,
)
from ..catalogs.results import effective_catalog_results
from ..models.identity import AstrometricSolution, ExternalIdentifier, Target
from ..models.catalogs import (
    IrasBandSelection,
    IrasDetectionFamily,
    NormalizedMeasurement,
    RawCatalogRow,
)
from ..providers import Astrometry
from .state import (
    SystemPhotometryState,
    load_system_photometry_state,
)
from ..catalogs.ubv_components import decode_ubv_component
from ..catalogs.tdsc_components import decode_tdsc_component
from ..targets import resolve_target
from ..vocabulary import INACTIVE_TARGET_STATES, TargetRole, TargetState
_AMBIGUOUS_SCOPES = {"ambiguous", "neighbour_context", "reject"}
_SIMBAD_IDENTIFIER_SOURCES = {"simbad", "simbad_metadata"}


def measurement_assignment_proposals(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    system_context: dict[str, object] | None = None,
    target_context_loader: Callable[[str | int], dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Propose, but do not persist, system-level measurement assignments.

    Proposals deliberately use only evidence already visible during review:
    exact identifiers, catalog-row positions, per-band resolution, SIMBAD
    component semantics, and audited target lifecycle state.  An ambiguous
    proposal never changes the materialized assignment table.
    """
    if system_context is None or target_context_loader is None:
        from ..hierarchy.system_context import HierarchySystemContextService
        from ..hierarchy.target_context import HierarchyTargetContextService

        if system_context is None:
            system_context = HierarchySystemContextService(
                session_factory,
            ).system_context(target_reference)
        if target_context_loader is None:
            target_context_loader = HierarchyTargetContextService(
                session_factory,
            ).target_context

    with session_factory() as session:
        requested = resolve_target(session, target_reference)
        if requested is None:
            raise KeyError(f"target not found: {target_reference}")
        targets = _proposal_targets(session, requested, system_context)
        if not targets:
            targets = {requested.id: requested}
        target_ids = sorted(targets)
        photometry_state = load_system_photometry_state(
            session, target_ids, expand_context=False,
        )
        measurements_by_id = dict(photometry_state.measurements)
        measurements = sorted(measurements_by_id.values(), key=lambda value: (
            value.provider, value.source_id, value.band, value.id,
        ))
        encounter_targets: dict[int, set[int]] = {}
        raw_rows = {}
        for encounter in photometry_state.encounters:
            encounter_targets.setdefault(encounter.measurement.id, set()).add(
                encounter.target_id
            )
            raw_rows.setdefault(encounter.measurement.id, encounter.raw_row)
        identifiers = _target_identifiers(session, target_ids)
        target_astrometry = _target_astrometry(session, targets)
        current = _current_assignments(
            photometry_state,
        )
        provenance_by_detection = {
            detection_id: [{
                "role": row.role,
                "service": row.service,
                "catalog_id": row.catalog_id,
                "table_id": row.table_id,
                "row_key": row.row_key,
                "identifier_column": row.identifier_column,
                "identifier_value": row.identifier_value,
                "source_url": row.source_url,
                "access_url": row.access_url,
                "readme_url": row.readme_url,
            } for row in rows]
            for detection_id, rows
            in photometry_state.catalog_provenance.items()
        }
        iras_family_by_measurement: dict[int, dict[str, object]] = {}
        iras_results = effective_catalog_results(
            session,
            target_ids,
            providers=("iras_psc", "iras_fsc"),
        )
        measurements_by_detection: dict[int, list[int]] = {}
        for measurement in measurements_by_id.values():
            measurements_by_detection.setdefault(
                measurement.detection_id, []
            ).append(measurement.id)
        families = list(session.scalars(select(IrasDetectionFamily).where(
            IrasDetectionFamily.target_id.in_(target_ids),
            IrasDetectionFamily.is_current.is_(True),
            IrasDetectionFamily.status == "associated",
        )))
        for family in families:
            psc_result = iras_results.get((family.target_id, "iras_psc"))
            fsc_result = iras_results.get((family.target_id, "iras_fsc"))
            if (
                psc_result is None
                or fsc_result is None
                or psc_result.run.id != family.psc_run_id
                or fsc_result.run.id != family.fsc_run_id
                or family.source_family_id is None
                or psc_result.selected_detection is None
                or fsc_result.selected_detection is None
            ):
                continue
            detection_ids = tuple(sorted((
                psc_result.selected_detection.id,
                fsc_result.selected_detection.id,
            )))
            family_key = "iras:" + ":".join(
                str(value) for value in detection_ids
            )
            selected_ids = set(session.scalars(
                select(IrasBandSelection.selected_measurement_id).where(
                    IrasBandSelection.family_id == family.source_family_id
                )
            ))
            for detection_id in detection_ids:
                for measurement_id in measurements_by_detection.get(
                    detection_id, ()
                ):
                    iras_family_by_measurement[measurement_id] = {
                        "key": family_key,
                        "family_id": family.source_family_id,
                        "reason": family.reason,
                        "normalized_separation": family.normalized_separation,
                        "selected_for_band": measurement_id in selected_ids,
                    }

    semantic = system_context.get("simbad_semantic_by_target") or {}
    lifecycle = system_context.get("target_lifecycle_by_target") or {}
    memberships = system_context.get("system_memberships_by_target") or {}
    target_contexts: dict[str, dict[str, object]] = {}
    result = []
    for measurement in measurements:
        raw = raw_rows.get(measurement.id)
        catalog_payload = _raw_catalog_payload(raw)
        if measurement.provider == "ubvmeans":
            catalog_component = decode_ubv_component(
                catalog_payload, measurement.source_id,
            )
        elif measurement.provider == "tdsc":
            catalog_component = decode_tdsc_component(
                catalog_payload, measurement.source_id,
            )
        else:
            catalog_component = None
        origin = _proposal_origin(
            measurement,
            encounter_targets.get(measurement.id, {measurement.target_id}),
            targets=targets,
            identifiers=identifiers,
            semantic=semantic,
            lifecycle=lifecycle,
            target_astrometry=target_astrometry,
            raw=raw,
            catalog_payload=catalog_payload,
        )
        origin_context = target_contexts.get(origin.sdbid)
        if origin_context is None:
            origin_context = target_context_loader(origin.sdbid)
            target_contexts[origin.sdbid] = origin_context
        prediction = _measurement_prediction(origin_context, measurement)
        source_position = Astrometry(
            origin.ra2000_deg if raw is None else raw.ra_deg,
            origin.dec2000_deg if raw is None else raw.dec_deg,
            2000.0 if raw is None else raw.epoch,
        )
        candidates = _candidate_rows(
            targets,
            source_position=source_position,
            target_astrometry=target_astrometry,
            provider=measurement.provider,
            source_id=measurement.source_id,
            identifiers=identifiers,
            semantic=semantic,
            lifecycle=lifecycle,
            memberships=memberships,
            catalog_payload=catalog_payload,
        )
        prediction = _effective_prediction(
            measurement,
            origin=origin,
            prediction=prediction,
            candidates=candidates,
        )
        proposed, proposal_reason, confidence = _propose_assignments(
            measurement,
            origin=origin,
            prediction=prediction,
            candidates=candidates,
            catalog_component=catalog_component,
        )
        current_rows = current.get(measurement.id, [])
        proposed_keys = {(row["target_id"], row["role"]) for row in proposed}
        current_keys = {(row["target_id"], row["role"]) for row in current_rows}
        if not proposed:
            comparison = "review_required"
        elif (
            any(row["role"] == "composite_scope" for row in proposed)
            and not any(row["role"] == "contributor" for row in proposed)
        ):
            comparison = "partial_proposal"
        elif not current_rows:
            comparison = "unassigned"
        elif proposed_keys == current_keys:
            comparison = "agrees_with_current"
        elif current_keys < proposed_keys:
            comparison = "partial_proposal"
        else:
            comparison = "differs_from_current"
        result.append({
            "measurement_id": measurement.id,
            "detection_id": measurement.detection_id,
            "origin_target_id": origin.id,
            "origin_sdbid": origin.sdbid,
            "encounter_target_ids": sorted(encounter_targets.get(measurement.id, ())),
            "encounter_sdbids": sorted(
                targets[target_id].sdbid
                for target_id in encounter_targets.get(measurement.id, ())
                if target_id in targets
            ),
            "provider": measurement.provider,
            "source_id": measurement.source_id,
            "source_display_name": catalog_source_display_name(
                measurement.provider,
                measurement.source_id,
                catalog_payload,
            ),
            "provenance": provenance_by_detection.get(
                measurement.detection_id, []
            ),
            "band": measurement.band,
            "value": measurement.value,
            "error": measurement.error,
            "systematic_error": measurement.systematic_error,
            "unit": measurement.unit,
            "upper_limit": measurement.upper_limit,
            "resolution_major_arcsec": measurement.resolution_major_arcsec,
            "resolution_minor_arcsec": measurement.resolution_minor_arcsec,
            "excluded": photometry_state.eligibility[
                measurement.id
            ].excluded,
            "exclusion_basis": photometry_state.eligibility[
                measurement.id
            ].basis,
            "exclusion_reason": photometry_state.eligibility[
                measurement.id
            ].reason,
            "predicted_scope": prediction["predicted_ownership_scope"],
            "predicted_blend_state": prediction["predicted_blend_state"],
            "scope_reason": prediction["scope_reason"],
            "catalog_component": (
                None if catalog_component is None else catalog_component.as_dict()
            ),
            "proposal_confidence": confidence,
            "proposal_reason": proposal_reason,
            "comparison_to_current": comparison,
            "proposed_assignments": proposed,
            "current_assignments": current_rows,
            "candidate_targets": candidates,
            "iras_family": iras_family_by_measurement.get(measurement.id),
        })
    return result


def _proposal_targets(
    session: Session,
    requested: Target,
    context: dict[str, object],
) -> dict[int, Target]:
    sdbids = {requested.sdbid}
    for row in context.get("component_positions") or []:
        if row.get("linked_sdbid"):
            sdbids.add(str(row["linked_sdbid"]))
    for row in context.get("measurement_assignments") or []:
        if row.get("origin_sdbid"):
            sdbids.add(str(row["origin_sdbid"]))
        for contributor in row.get("contributors") or []:
            if contributor.get("sdbid"):
                sdbids.add(str(contributor["sdbid"]))
    lifecycle = context.get("target_lifecycle_by_target") or {}
    sdbids.update(str(value) for value in lifecycle)
    return {
        target.id: target
        for target in session.scalars(select(Target).where(Target.sdbid.in_(sdbids)))
    }


def _proposal_origin(
    measurement: NormalizedMeasurement,
    encountered_target_ids: set[int],
    *,
    targets: dict[int, Target],
    identifiers: dict[int, tuple[tuple[str, str], ...]],
    semantic: dict[str, dict[str, object]],
    lifecycle: dict[str, dict[str, object]],
    target_astrometry: dict[int, Astrometry],
    raw: RawCatalogRow | None,
    catalog_payload: dict[str, object] | None,
) -> Target:
    encountered = [
        targets[target_id]
        for target_id in encountered_target_ids
        if target_id in targets
    ]
    if not encountered:
        return targets.get(measurement.target_id) or next(iter(targets.values()))
    identifier_matches = []
    for target in encountered:
        sources = _matching_identifier_sources(
            measurement.provider,
            measurement.source_id,
            identifiers.get(target.id, ()),
            catalog_payload=catalog_payload,
        )
        if sources:
            identifier_matches.append((target, _identifier_authority(sources)))
    if identifier_matches:
        strongest = max(authority for _target, authority in identifier_matches)
        preferred = [
            target for target, authority in identifier_matches
            if authority == strongest
        ]
        if len(preferred) == 1:
            return preferred[0]
    roles = {
        target.id: effective_target_role(
            lifecycle.get(target.sdbid) or {},
            semantic.get(target.sdbid) or {},
        )[0]
        for target in encountered
    }
    if measurement.ownership_scope in {"system", "shared"}:
        composites = [
            target
            for target in encountered
            if roles[target.id] == TargetRole.COMPOSITE
        ]
        if len(composites) == 1:
            return composites[0]
    physical = [
        target
        for target in encountered
        if roles[target.id] == TargetRole.PHYSICAL
    ]
    if raw is not None and physical:
        source = Astrometry(raw.ra_deg, raw.dec_deg, raw.epoch)
        return min(physical, key=lambda target: angular_separation_arcsec(
            source, target_astrometry[target.id], epoch=raw.epoch,
        ))
    return min(encountered, key=lambda target: target.sdbid)


def _target_identifiers(
    session: Session, target_ids: list[int]
) -> dict[int, tuple[tuple[str, str], ...]]:
    values: dict[int, list[tuple[str, str]]] = {
        target_id: [] for target_id in target_ids
    }
    for row in session.scalars(select(ExternalIdentifier).where(
        ExternalIdentifier.target_id.in_(target_ids)
    )):
        values[row.target_id].append((row.value, row.source))
    return {target_id: tuple(rows) for target_id, rows in values.items()}


def _matching_identifier_sources(
    provider: str,
    source_id: str,
    identifiers: tuple[tuple[str, str], ...],
    *,
    catalog_payload: dict[str, object] | None = None,
) -> tuple[str, ...]:
    """Return the provenance of exact catalog identifiers on one target."""
    return tuple(sorted({
        source
        for value, source in identifiers
        if catalog_source_id_matches_identifiers(
            provider,
            source_id,
            (value,),
            payload=catalog_payload,
        )
    }))


def _identifier_authority(sources: tuple[str, ...]) -> int:
    """Rank identifier provenance without hiding lower-authority matches.

    SIMBAD identifiers describe the identity of the named astronomical object.
    Provider-derived identifiers may instead have been attached by an earlier
    positional match, which must not outrank SIMBAD when both occur elsewhere
    in the same imported system.
    """
    return 2 if _SIMBAD_IDENTIFIER_SOURCES.intersection(sources) else 1


def _raw_catalog_payload(raw: RawCatalogRow | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw.payload_json)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _target_astrometry(
    session: Session,
    targets: dict[int, Target],
) -> dict[int, Astrometry]:
    solution_ids = {
        target.canonical_astrometry_id
        for target in targets.values()
        if target.canonical_astrometry_id is not None
    }
    solutions = {
        solution.id: solution
        for solution in session.scalars(select(AstrometricSolution).where(
            AstrometricSolution.id.in_(solution_ids)
        ))
    }
    result = {}
    for target in targets.values():
        solution = solutions.get(target.canonical_astrometry_id)
        result[target.id] = Astrometry(
            target.ra2000_deg,
            target.dec2000_deg,
            2000.0,
            pm_ra_cosdec_masyr=None if solution is None else solution.pm_ra_cosdec_masyr,
            pm_dec_masyr=None if solution is None else solution.pm_dec_masyr,
            source="sdb" if solution is None else solution.source,
        )
    return result


def _current_assignments(
    state: SystemPhotometryState,
) -> dict[int, list[dict[str, object]]]:
    result: dict[int, list[dict[str, object]]] = {}
    for row in state.assignments:
        result.setdefault(row.measurement_id, []).append({
            "association_id": row.association_id,
            "target_id": row.target_id,
            "sdbid": (
                state.referenced_targets[row.target_id].sdbid
                if row.target_id in state.referenced_targets
                else None
            ),
            "role": row.role,
            "method": row.method,
            "weight": row.weight,
            "derived": row.derived,
        })
    return result


def _measurement_prediction(
    context: dict[str, object],
    measurement: NormalizedMeasurement,
) -> dict[str, str]:
    bands = context.get("photometry_context", {}).get("bands", [])
    for row in bands:
        if row.get("provider") == measurement.provider and row.get("band") == measurement.band:
            return {
                "predicted_ownership_scope": str(row.get("predicted_ownership_scope") or "component"),
                "predicted_blend_state": str(row.get("predicted_blend_state") or "unknown"),
                "scope_reason": str(row.get("scope_reason") or ""),
            }
    return {
        "predicted_ownership_scope": measurement.ownership_scope,
        "predicted_blend_state": measurement.blend_state,
        "scope_reason": "no hierarchy band prediction was available; retained stored catalog scope",
    }


def _candidate_rows(
    targets: dict[int, Target],
    *,
    source_position: Astrometry,
    target_astrometry: dict[int, Astrometry],
    provider: str,
    source_id: str,
    identifiers: dict[int, tuple[tuple[str, str], ...]],
    semantic: dict[str, dict[str, object]],
    lifecycle: dict[str, dict[str, object]],
    memberships: dict[str, list[dict[str, object]]],
    catalog_payload: dict[str, object] | None,
) -> list[dict[str, object]]:
    rows = []
    for target in targets.values():
        lifecycle_row = lifecycle.get(target.sdbid) or {}
        semantic_row = semantic.get(target.sdbid) or {}
        role, role_basis = effective_target_role(lifecycle_row, semantic_row)
        state = str(lifecycle_row.get("state") or TargetState.ACTIVE)
        identifier_sources = _matching_identifier_sources(
            provider,
            source_id,
            identifiers.get(target.id, ()),
            catalog_payload=catalog_payload,
        )
        rows.append({
            "target_id": target.id,
            "sdbid": target.sdbid,
            "target_role": role,
            "target_role_basis": role_basis,
            "target_state": state,
            "eligible": state not in INACTIVE_TARGET_STATES,
            "identifier_match": bool(identifier_sources),
            "identifier_sources": list(identifier_sources),
            "identifier_authority": _identifier_authority(identifier_sources)
            if identifier_sources else 0,
            "system_memberships": list(memberships.get(target.sdbid) or []),
            "separation_arcsec": angular_separation_arcsec(
                source_position,
                target_astrometry[target.id],
                epoch=source_position.epoch,
            ),
            "comparison_epoch": source_position.epoch,
        })
    strongest_authority = max(
        (int(row["identifier_authority"]) for row in rows), default=0
    )
    for row in rows:
        row["identifier_preferred"] = bool(
            row["identifier_match"]
            and int(row["identifier_authority"]) == strongest_authority
        )
    return sorted(rows, key=lambda row: (
        not bool(row["identifier_preferred"]),
        not bool(row["identifier_match"]),
        float(row["separation_arcsec"]),
        str(row["sdbid"]),
    ))


def _effective_prediction(
    measurement: NormalizedMeasurement,
    *,
    origin: Target,
    prediction: dict[str, str],
    candidates: list[dict[str, object]],
) -> dict[str, str]:
    """Add explicit-system lifecycle evidence absent from provider hierarchy."""
    if prediction["predicted_ownership_scope"] != "component":
        return prediction
    resolution = measurement.resolution_major_arcsec
    if resolution is None:
        return prediction
    composites = [
        row for row in candidates
        if row["eligible"] and row["target_role"] == TargetRole.COMPOSITE
    ]
    if len(composites) != 1:
        return prediction
    physical_in_beam = [
        row for row in candidates
        if row["eligible"]
        and row["target_role"] == TargetRole.PHYSICAL
        and row["separation_arcsec"] <= resolution
    ]
    if len(physical_in_beam) < 2:
        return prediction
    return {
        "predicted_ownership_scope": "system",
        "predicted_blend_state": "blended",
        "predicted_blend_reason": "unresolved_at_catalog_resolution",
        "scope_reason": (
            "one audited composite scope exists and at least two physical "
            "system members lie within one stored full-width resolution"
        ),
    }


def effective_target_role(
    lifecycle: dict[str, object],
    semantic: dict[str, object],
) -> tuple[str, str]:
    explicit = str(lifecycle.get("role") or TargetRole.UNSPECIFIED)
    if explicit != TargetRole.UNSPECIFIED:
        return explicit, "target_lifecycle"
    kind = str(semantic.get("kind") or "unknown")
    labels = semantic.get("component_label_candidates") or []
    label = "" if not labels else str(labels[0].get("label") or "")
    if kind == "system_or_parent" or _group_component_label(label):
        return TargetRole.COMPOSITE.value, "simbad_semantics"
    return TargetRole.PHYSICAL.value, "default_or_component_semantics"


def _group_component_label(value: str) -> bool:
    value = value.strip()
    return bool("," in value or (len(value) > 1 and value.isalpha() and value.isupper()))


def _propose_assignments(
    measurement: NormalizedMeasurement,
    *,
    origin: Target,
    prediction: dict[str, str],
    candidates: list[dict[str, object]],
    catalog_component=None,
) -> tuple[list[dict[str, object]], str, str]:
    scope = prediction["predicted_ownership_scope"]
    eligible = [row for row in candidates if row["eligible"]]
    physical = [
        row for row in eligible if row["target_role"] == TargetRole.PHYSICAL
    ]
    composites = [
        row for row in eligible if row["target_role"] == TargetRole.COMPOSITE
    ]
    identifier_physical = [row for row in physical if row["identifier_preferred"]]
    identifier_composite = [row for row in composites if row["identifier_preferred"]]
    simbad_identifier_composite = [
        row for row in identifier_composite
        if _SIMBAD_IDENTIFIER_SOURCES.intersection(
            row.get("identifier_sources") or []
        )
    ]

    if scope in _AMBIGUOUS_SCOPES:
        return [], f"predicted scope {scope} requires review", "low"

    if scope == "component":
        if (
            catalog_component is not None
            and catalog_component.kind in {
                "component_ordinal", "named_component",
            }
        ):
            component_rows = _component_label_candidates(
                physical, catalog_component.component_label or "",
            )
            tolerance = max(
                1.0,
                min(3.0, (measurement.resolution_major_arcsec or 2.0) / 2.0),
            )
            corroborated = [
                row for row in component_rows
                if row["identifier_preferred"]
                or row["separation_arcsec"] <= tolerance
            ]
            conflicting_identifiers = [
                row for row in identifier_physical
                if row not in component_rows
            ]
            if conflicting_identifiers:
                return [], (
                    f"catalog component {catalog_component.native_code} "
                    f"({catalog_component.component_label}) conflicts with "
                    "the preferred exact target identifier"
                ), "low"
            if len(corroborated) == 1:
                row = corroborated[0]
                evidence = "catalog_component_code+system_membership"
                if row["identifier_preferred"]:
                    evidence += f"+{_identifier_evidence(row)}"
                    confidence = "high"
                    reason = "preferred exact identifier"
                else:
                    evidence += "+position"
                    confidence = "medium"
                    reason = f"position within {tolerance:.2f} arcsec"
                return [_proposal(row, "contributor", evidence)], (
                    f"catalog component {catalog_component.native_code} maps "
                    f"to system member {catalog_component.component_label}, "
                    f"corroborated by {reason}"
                ), confidence
            if len(corroborated) > 1:
                return [], (
                    f"catalog component {catalog_component.native_code} maps "
                    "to more than one corroborated system member"
                ), "low"
        if len(identifier_physical) == 1:
            return [_proposal(
                identifier_physical[0],
                "contributor",
                _identifier_evidence(identifier_physical[0]),
            )], (
                "one physical target has the preferred exact source identifier "
                f"({_identifier_source_label(identifier_physical[0])})"
            ), "high"
        if len(identifier_physical) > 1:
            return [], "the catalog source identifier belongs to multiple physical targets", "low"
        if len(simbad_identifier_composite) == 1:
            row = simbad_identifier_composite[0]
            return [_proposal(row, "composite_scope", "simbad_identifier")], (
                "the resolved catalog source has an exact SIMBAD identifier for "
                "the imported composite; its physical component contributor is not imported"
            ), "high"
        if len(simbad_identifier_composite) > 1:
            return [], (
                "the exact SIMBAD source identifier belongs to multiple imported composites"
            ), "low"
        tolerance = max(1.0, min(3.0, (measurement.resolution_major_arcsec or 2.0) / 2.0))
        positional = [row for row in physical if row["separation_arcsec"] <= tolerance]
        if len(positional) == 1:
            return [_proposal(positional[0], "contributor", "position")], (
                f"one physical target lies within the {tolerance:.2f} arcsec resolved-source tolerance"
            ), "medium"
        if len(positional) > 1:
            nearest = positional[0]
            if len(positional) == 1 or nearest["separation_arcsec"] + 0.5 < positional[1]["separation_arcsec"]:
                return [_proposal(nearest, "contributor", "position")], (
                    "nearest physical target is at least 0.5 arcsec closer than the alternative"
                ), "medium"
            return [], "multiple physical targets are positionally plausible for a resolved source", "low"
        origin_row = next((row for row in physical if row["target_id"] == origin.id), None)
        if origin_row is not None:
            return [_proposal(origin_row, "contributor", "origin_fallback")], (
                "no imported system member matches closely; retained the physical origin target"
            ), "low"
        return [], "resolved measurement has no imported physical target at its catalog position", "low"

    if scope in {"system", "shared"}:
        beam = measurement.resolution_major_arcsec
        composite_scopes = identifier_composite or [
            row for row in composites if row["target_id"] == origin.id
        ]
        if (
            catalog_component is not None
            and catalog_component.kind == "combined_components"
            and len(identifier_composite) == 1
        ):
            contributors = _simple_binary_contributors(
                identifier_composite[0], physical,
            )
            if contributors is not None:
                assignments = [
                    _proposal(
                        row,
                        "contributor",
                        "catalog_component_D+simple_binary_membership",
                    )
                    for row in contributors
                ]
                assignments.append(_proposal(
                    identifier_composite[0],
                    "composite_scope",
                    f"catalog_component_D+{_identifier_evidence(identifier_composite[0])}",
                ))
                return assignments, (
                    "catalog component D records combined light; the exact "
                    "composite identifier and a unique simple A+B system "
                    "identify both physical contributors"
                ), "high"
        if beam is None:
            assignments = [
                _proposal(row, "composite_scope", _identifier_evidence(row) if row["identifier_preferred"] else "origin_scope")
                for row in composite_scopes
            ]
            return assignments, (
                "the composite scope is identifiable, but missing resolution prevents "
                "selection of physical contributors"
            ), "low"
        contributors = [row for row in physical if row["separation_arcsec"] <= beam]
        assignments = [
            _proposal(
                row,
                "contributor",
                f"{_identifier_evidence(row)}+beam"
                if row["identifier_preferred"] else "beam",
            )
            for row in contributors
        ]
        assignments.extend(
            _proposal(
                row,
                "composite_scope",
                _identifier_evidence(row)
                if row["identifier_preferred"] else "origin_scope",
            )
            for row in composite_scopes
        )
        if not assignments:
            return [], f"no eligible system target lies within the {beam:.2f} arcsec review beam", "low"
        if not contributors:
            simbad_scopes = [
                row for row in composite_scopes
                if _SIMBAD_IDENTIFIER_SOURCES.intersection(
                    row.get("identifier_sources") or []
                )
            ]
            if len(simbad_scopes) == 1:
                return assignments, (
                    "the composite scope has an exact SIMBAD source identifier; "
                    f"association is secure, but no imported physical contributor lies "
                    f"within the {beam:.2f} arcsec review beam"
                ), "high"
            return assignments, (
                f"the composite scope is identifiable, but no imported physical contributor "
                f"lies within the {beam:.2f} arcsec review beam"
            ), "low"
        confidence = "high" if identifier_physical or identifier_composite else "medium"
        reason = (
            f"physical targets within one stored full-width resolution "
            f"({beam:.2f} arcsec) contribute"
        )
        if composite_scopes:
            reason += "; the identified/origin composite is retained as measurement scope"
        preferred_identifiers = [*identifier_physical, *identifier_composite]
        if preferred_identifiers:
            provenance = sorted({
                _identifier_source_label(row) for row in preferred_identifiers
            })
            reason += "; preferred exact identifier provenance: " + ", ".join(provenance)
        return assignments, reason, confidence

    return [], f"unsupported predicted scope {scope}", "low"


def _component_label_candidates(
    rows: list[dict[str, object]],
    component_label: str,
) -> list[dict[str, object]]:
    wanted = component_label.strip().upper()
    return [
        row for row in rows
        if any(
            str(membership.get("component_label") or "").strip().upper()
            == wanted
            for membership in row.get("system_memberships") or []
        )
    ]


def _simple_binary_contributors(
    composite: dict[str, object],
    physical: list[dict[str, object]],
) -> list[dict[str, object]] | None:
    composite_system_ids = {
        int(membership["system_id"])
        for membership in composite.get("system_memberships") or []
        if str(membership.get("component_label") or "").strip().upper() == "AB"
    }
    solutions = []
    for system_id in composite_system_ids:
        members: dict[str, list[dict[str, object]]] = {}
        for row in physical:
            labels = {
                str(membership.get("component_label") or "").strip().upper()
                for membership in row.get("system_memberships") or []
                if int(membership.get("system_id") or -1) == system_id
            }
            for label in labels:
                members.setdefault(label, []).append(row)
        if set(members) == {"A", "B"} and all(
            len(members[label]) == 1 for label in ("A", "B")
        ):
            solutions.append([members["A"][0], members["B"][0]])
    return solutions[0] if len(solutions) == 1 else None


def _proposal(row: dict[str, object], role: str, evidence: str) -> dict[str, object]:
    return {
        "target_id": row["target_id"],
        "sdbid": row["sdbid"],
        "role": role,
        "evidence": evidence,
        "identifier_match": row["identifier_match"],
        "identifier_preferred": row["identifier_preferred"],
        "identifier_sources": row["identifier_sources"],
        "separation_arcsec": row["separation_arcsec"],
    }


def _identifier_source_label(row: dict[str, object]) -> str:
    sources = [str(value) for value in row.get("identifier_sources") or []]
    return ", ".join(sources) if sources else "unknown provenance"


def _identifier_evidence(row: dict[str, object]) -> str:
    return (
        "simbad_identifier"
        if _SIMBAD_IDENTIFIER_SOURCES.intersection(
            row.get("identifier_sources") or []
        )
        else "identifier"
    )
