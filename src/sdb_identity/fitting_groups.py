from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .adapters import catalog_source_display_name
from .catalog_measurements import current_measurement_encounters
from .dirty import find_target
from .effective_assignments import effective_measurement_assignments
from .models import (
    CatalogDetectionProvenance,
    MeasurementTargetAssociation,
    NormalizedMeasurement,
    PhotometryOverride,
    RawCatalogRow,
    Target,
    TargetLifecycleAction,
    TargetSystem,
    TargetSystemMember,
)
from .samples import SampleService


_INACTIVE_STATES = {"suppressed", "superseded", "archived"}


def fitting_group_report(
    session_factory: sessionmaker[Session],
    *,
    target_reference: str | int | None = None,
    sample: str | None = None,
) -> dict[str, object]:
    """Derive read-only joint-fitting groups from accepted assignments."""
    if (target_reference is None) == (sample is None):
        raise ValueError("specify exactly one target or --sample")
    with session_factory() as session:
        selected = _selected_targets(
            session, session_factory,
            target_reference=target_reference, sample=sample,
        )
        context_target_ids, assigned_measurement_ids = _context_closure(
            session, set(selected)
        )
        encounter_target_ids = _current_measurement_encounters(
            session, context_target_ids
        )
        current_measurement_ids = set(encounter_target_ids)
        measurement_ids = assigned_measurement_ids | current_measurement_ids
        targets = {
            row.id: row for row in _scalars_in(
                session, Target, Target.id, context_target_ids
            )
        }
        measurements = {
            row.id: row for row in _scalars_in(
                session, NormalizedMeasurement, NormalizedMeasurement.id,
                measurement_ids,
            )
        }
        provenance_by_detection: dict[int, list[dict[str, object]]] = defaultdict(list)
        detection_ids = {
            row.detection_id for row in measurements.values()
        }
        for provenance in _scalars_in(
            session,
            CatalogDetectionProvenance,
            CatalogDetectionProvenance.detection_id,
            detection_ids,
        ):
            provenance_by_detection[provenance.detection_id].append({
                "role": provenance.role,
                "service": provenance.service,
                "catalog_id": provenance.catalog_id,
                "table_id": provenance.table_id,
                "row_key": provenance.row_key,
                "identifier_column": provenance.identifier_column,
                "identifier_value": provenance.identifier_value,
                "source_url": provenance.source_url,
                "access_url": provenance.access_url,
                "readme_url": provenance.readme_url,
            })
        raw_payloads = {}
        raw_row_ids = {
            row.raw_row_id for row in measurements.values()
            if row.raw_row_id is not None
        }
        for raw in _scalars_in(
            session, RawCatalogRow, RawCatalogRow.id, raw_row_ids
        ):
            try:
                payload = json.loads(raw.payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                raw_payloads[raw.id] = payload
        associations = effective_measurement_assignments(
            session,
            measurement_ids,
        )
        lifecycle = _lifecycle(session, context_target_ids)
        systems = _system_context(session, context_target_ids)
        overrides = _overrides(
            session, {row.target_id for row in measurements.values()}
        )

    target_rows = {}
    model_target_ids = set()
    for target_id, target in targets.items():
        status = lifecycle.get(target_id, {
            "role": "unspecified", "state": "active",
            "action_id": None,
        })
        model_target = (
            status["role"] != "composite"
            and status["state"] not in _INACTIVE_STATES
        )
        if model_target:
            model_target_ids.add(target_id)
        target_rows[target_id] = {
            "target_id": target_id,
            "sdbid": target.sdbid,
            "role": status["role"],
            "state": status["state"],
            "model_target": model_target,
            "role_review_required": status["role"] == "unspecified",
            "selected": target_id in selected,
            "systems": systems.get(target_id, []),
        }

    associations_by_measurement = defaultdict(list)
    for row in associations:
        associations_by_measurement[row.measurement_id].append(row)

    measurement_rows = {}
    for measurement_id, measurement in measurements.items():
        assigned = associations_by_measurement.get(measurement_id, [])
        contributor_ids = sorted({
            row.target_id for row in assigned if row.role == "contributor"
        })
        active_contributor_ids = [
            target_id for target_id in contributor_ids
            if target_id in model_target_ids
        ]
        composite_scope_ids = sorted({
            row.target_id for row in assigned if row.role == "composite_scope"
        })
        excluded, exclusion_basis = _effective_exclusion(
            measurement,
            overrides=overrides,
        )
        fit_enabled = not excluded and bool(active_contributor_ids)
        flags = []
        if not assigned:
            flags.append("no_current_assignment")
        if measurement_id not in current_measurement_ids:
            flags.append("not_currently_encountered")
        if composite_scope_ids and not active_contributor_ids:
            scope_roles = {
                target_rows[target_id]["role"]
                for target_id in composite_scope_ids
                if target_id in target_rows
            }
            if "composite" in scope_roles:
                flags.append("composite_scope_without_physical_contributor")
            if "unspecified" in scope_roles:
                flags.append("scope_assignment_requires_target_role_review")
            if "physical" in scope_roles:
                flags.append("physical_target_assigned_as_composite_scope")
        if set(contributor_ids) - set(active_contributor_ids):
            flags.append("inactive_or_nonphysical_contributor")
        measurement_rows[measurement_id] = {
            "measurement_id": measurement_id,
            "detection_id": measurement.detection_id,
            "raw_row_id": measurement.raw_row_id,
            "origin_target_id": measurement.target_id,
            "origin_sdbid": targets.get(measurement.target_id).sdbid
            if measurement.target_id in targets else None,
            "encounter_target_ids": sorted(
                encounter_target_ids.get(measurement_id, set())
            ),
            "encounter_sdbids": [
                targets[target_id].sdbid
                for target_id in sorted(encounter_target_ids.get(measurement_id, set()))
                if target_id in targets
            ],
            "provider": measurement.provider,
            "source_id": measurement.source_id,
            "source_display_name": catalog_source_display_name(
                measurement.provider,
                measurement.source_id,
                raw_payloads.get(measurement.raw_row_id),
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
            "private": measurement.private,
            "bibcode": measurement.bibcode,
            "ownership_scope": measurement.ownership_scope,
            "blend_state": measurement.blend_state,
            "blend_reason": measurement.blend_reason,
            "resolution_major_arcsec": measurement.resolution_major_arcsec,
            "resolution_minor_arcsec": measurement.resolution_minor_arcsec,
            "resolution_kind": measurement.resolution_kind,
            "resolution_reference": measurement.resolution_reference,
            "provider_excluded": measurement.excluded,
            "fit_excluded": excluded,
            "exclusion_basis": exclusion_basis,
            "fit_enabled": fit_enabled,
            "current_encounter": measurement_id in current_measurement_ids,
            "contributor_target_ids": contributor_ids,
            "contributor_sdbids": [
                targets[target_id].sdbid for target_id in contributor_ids
                if target_id in targets
            ],
            "active_model_contributor_ids": active_contributor_ids,
            "composite_scope_target_ids": composite_scope_ids,
            "composite_scope_sdbids": [
                targets[target_id].sdbid for target_id in composite_scope_ids
                if target_id in targets
            ],
            "assignments": [{
                "target_id": association.target_id,
                "sdbid": targets[association.target_id].sdbid
                if association.target_id in targets else None,
                "role": association.role,
                "method": association.method,
                "weight": association.weight,
                "note": association.note,
                "association_id": association.association_id,
                "derived": association.derived,
            } for association in sorted(
                assigned,
                key=lambda value: (
                    value.role,
                    value.target_id,
                    value.association_id or 0,
                ),
            )],
            "review_flags": flags,
        }

    parent = {target_id: target_id for target_id in model_target_ids}

    def find(target_id: int) -> int:
        while parent[target_id] != target_id:
            parent[target_id] = parent[parent[target_id]]
            target_id = parent[target_id]
        return target_id

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[max(first_root, second_root)] = min(first_root, second_root)

    for row in measurement_rows.values():
        contributors = row["active_model_contributor_ids"]
        if not row["fit_enabled"] or len(contributors) < 2:
            continue
        for target_id in contributors[1:]:
            union(contributors[0], target_id)

    members_by_root = defaultdict(list)
    for target_id in sorted(model_target_ids):
        members_by_root[find(target_id)].append(target_id)

    group_by_target = {}
    groups = []
    for member_ids in sorted(
        members_by_root.values(),
        key=lambda values: [targets[value].sdbid for value in values],
    ):
        sdbids = sorted(targets[value].sdbid for value in member_ids)
        group_id = _group_id(sdbids)
        for target_id in member_ids:
            group_by_target[target_id] = group_id
        groups.append({
            "group_id": group_id,
            "target_ids": member_ids,
            "sdbids": sdbids,
            "fit_measurement_ids": [],
            "context_measurement_ids": [],
            "composite_scope_target_ids": [],
            "review_flags": [
                "unspecified_target_role"
                for target_id in member_ids
                if target_rows[target_id]["role_review_required"]
            ][:1],
        })
    groups_by_id = {row["group_id"]: row for row in groups}

    validation_errors = []
    unresolved = []
    scope_role_review = []
    for row in measurement_rows.values():
        contributor_groups = sorted({
            group_by_target[target_id]
            for target_id in row["active_model_contributor_ids"]
            if target_id in group_by_target
        })
        row["fitting_group_ids"] = contributor_groups
        if row["fit_enabled"] and len(contributor_groups) != 1:
            validation_errors.append(
                f"fit-enabled measurement {row['measurement_id']} spans "
                f"{len(contributor_groups)} fitting groups"
            )
        for group_id in contributor_groups:
            group = groups_by_id[group_id]
            key = "fit_measurement_ids" if row["fit_enabled"] else "context_measurement_ids"
            group[key].append(row["measurement_id"])
            group["composite_scope_target_ids"].extend(
                row["composite_scope_target_ids"]
            )
        if "composite_scope_without_physical_contributor" in row["review_flags"]:
            unresolved.append({
                "measurement_id": row["measurement_id"],
                "provider": row["provider"],
                "band": row["band"],
                "composite_scope_sdbids": row["composite_scope_sdbids"],
                "reason": "secure composite scope has no active physical contributor assignment",
            })
        if "scope_assignment_requires_target_role_review" in row["review_flags"]:
            scope_role_review.append({
                "measurement_id": row["measurement_id"],
                "provider": row["provider"],
                "band": row["band"],
                "scope_sdbids": row["composite_scope_sdbids"],
                "reason": "composite-scope assignment belongs to a target whose physical/composite role is unspecified",
            })

    for group in groups:
        group["fit_measurement_ids"] = sorted(set(group["fit_measurement_ids"]))
        group["context_measurement_ids"] = sorted(set(group["context_measurement_ids"]))
        group["composite_scope_target_ids"] = sorted(set(
            group["composite_scope_target_ids"]
        ))
        group["composite_scope_sdbids"] = [
            targets[target_id].sdbid
            for target_id in group["composite_scope_target_ids"]
            if target_id in targets
        ]

    measurement_values = sorted(
        measurement_rows.values(),
        key=lambda row: (
            row["provider"],
            row["detection_id"],
            row["band"],
            row["measurement_id"],
        ),
    )
    return {
        "selection": {
            "target": None if target_reference is None else str(target_reference),
            "sample": sample,
            "selected_sdbids": [targets[value].sdbid for value in sorted(selected)],
        },
        "summary": {
            "context_target_count": len(target_rows),
            "model_target_count": len(model_target_ids),
            "composite_target_count": sum(
                row["role"] == "composite" for row in target_rows.values()
            ),
            "fitting_group_count": len(groups),
            "measurement_count": len(measurement_values),
            "fit_enabled_measurement_count": sum(
                row["fit_enabled"] for row in measurement_values
            ),
            "excluded_context_measurement_count": sum(
                row["fit_excluded"] for row in measurement_values
            ),
            "multi_contributor_measurement_count": sum(
                len(row["active_model_contributor_ids"]) > 1
                for row in measurement_values if row["fit_enabled"]
            ),
            "unresolved_composite_measurement_count": len(unresolved),
            "scope_role_review_measurement_count": len(scope_role_review),
            "unassigned_measurement_count": sum(
                "no_current_assignment" in row["review_flags"]
                for row in measurement_values
            ),
        },
        "targets": [target_rows[value] for value in sorted(
            target_rows, key=lambda target_id: target_rows[target_id]["sdbid"]
        )],
        "groups": groups,
        "measurements": measurement_values,
        "unresolved_composite_measurements": unresolved,
        "scope_role_review_measurements": scope_role_review,
        "invariants": {
            "valid": not validation_errors,
            "errors": validation_errors,
            "fit_enabled_measurements_belong_to_one_group": not validation_errors,
            "composite_targets_are_not_model_nodes": all(
                target_id not in model_target_ids
                for target_id, row in target_rows.items()
                if row["role"] == "composite"
            ),
        },
        "notes": [
            "read-only graph uses explicit assignments or one accepted source association",
            "excluded measurements remain visible but do not connect fitting groups",
            "hierarchy membership adds context but does not itself require joint fitting",
            "legacy IPAC export and SDF behavior are unchanged",
        ],
    }


def _selected_targets(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    target_reference: str | int | None,
    sample: str | None,
) -> set[int]:
    if sample is not None:
        return {row.id for row in SampleService(session_factory).members(sample)}
    target = find_target(session, target_reference)
    if target is None:
        raise KeyError(f"target not found: {target_reference}")
    return {target.id}


def _context_closure(
    session: Session, selected_target_ids: set[int]
) -> tuple[set[int], set[int]]:
    target_ids = set(selected_target_ids)
    measurement_ids: set[int] = set()
    changed = True
    while changed:
        changed = False
        system_ids = set()
        for chunk in _chunks(target_ids):
            system_ids.update(session.scalars(
                select(TargetSystemMember.system_id).where(
                    TargetSystemMember.target_id.in_(chunk)
                )
            ))
        new_targets = set()
        for chunk in _chunks(system_ids):
            new_targets.update(session.scalars(
                select(TargetSystemMember.target_id).where(
                    TargetSystemMember.system_id.in_(chunk)
                )
            ))
        frontier_targets = target_ids | new_targets
        new_measurements = set()
        for chunk in _chunks(frontier_targets):
            new_measurements.update(session.scalars(
                select(MeasurementTargetAssociation.measurement_id).where(
                    MeasurementTargetAssociation.target_id.in_(chunk)
                )
            ))
        all_measurements = measurement_ids | new_measurements
        associated_targets = set()
        for chunk in _chunks(all_measurements):
            associated_targets.update(session.scalars(
                select(MeasurementTargetAssociation.target_id).where(
                    MeasurementTargetAssociation.measurement_id.in_(chunk)
                )
            ))
        expanded_targets = frontier_targets | associated_targets
        if expanded_targets != target_ids or all_measurements != measurement_ids:
            changed = True
            target_ids = expanded_targets
            measurement_ids = all_measurements
    return target_ids, measurement_ids


def _current_measurement_encounters(
    session: Session, target_ids: set[int]
) -> dict[int, set[int]]:
    result: dict[int, set[int]] = defaultdict(set)
    for chunk in _chunks(target_ids):
        for row in current_measurement_encounters(session, chunk):
            result[row.measurement.id].add(row.target_id)
    return dict(result)


def _lifecycle(session: Session, target_ids: set[int]) -> dict[int, dict[str, object]]:
    result = {}
    for chunk in _chunks(target_ids):
        latest = (
            select(
                TargetLifecycleAction.target_id,
                func.max(TargetLifecycleAction.id).label("action_id"),
            )
            .where(TargetLifecycleAction.target_id.in_(chunk))
            .group_by(TargetLifecycleAction.target_id)
            .subquery()
        )
        for row in session.scalars(
            select(TargetLifecycleAction).join(
                latest, TargetLifecycleAction.id == latest.c.action_id
            )
        ):
            result[row.target_id] = {
                "role": row.role, "state": row.state, "action_id": row.id,
            }
    return result


def _system_context(
    session: Session, target_ids: set[int]
) -> dict[int, list[dict[str, object]]]:
    result = defaultdict(list)
    for chunk in _chunks(target_ids):
        rows = session.execute(
            select(TargetSystemMember, TargetSystem)
            .join(TargetSystem, TargetSystem.id == TargetSystemMember.system_id)
            .where(TargetSystemMember.target_id.in_(chunk))
            .order_by(TargetSystem.name, TargetSystemMember.id)
        )
        for member, system in rows:
            result[member.target_id].append({
                "system_id": system.id,
                "name": system.name,
                "component_label": member.component_label,
                "primary": system.primary_target_id == member.target_id,
            })
    return dict(result)


def _overrides(
    session: Session, target_ids: set[int]
) -> dict[tuple[int, str, str], PhotometryOverride]:
    result = {}
    for chunk in _chunks(target_ids):
        for row in session.scalars(
            select(PhotometryOverride)
            .where(PhotometryOverride.target_id.in_(chunk))
            .order_by(PhotometryOverride.id)
        ):
            result[(row.target_id, row.provider, row.band)] = row
    return result


def _effective_exclusion(
    measurement: NormalizedMeasurement,
    *,
    overrides: dict[tuple[int, str, str], PhotometryOverride],
) -> tuple[bool, str]:
    override = overrides.get((
        measurement.target_id, measurement.provider.lower(), measurement.band.upper()
    ))
    excluded = measurement.excluded if override is None else override.excluded
    basis = "provider_excluded" if measurement.excluded else "included"
    if override is not None:
        basis = "manual_exclude_override" if override.excluded else "manual_include_override"
    return excluded, basis


def _group_id(sdbids: list[str]) -> str:
    digest = hashlib.sha256("\n".join(sdbids).encode("utf-8")).hexdigest()[:12]
    return f"fit-group-{digest}"


def _scalars_in(session: Session, model, column, values: set[int]):
    rows = []
    for chunk in _chunks(values):
        rows.extend(session.scalars(select(model).where(column.in_(chunk))))
    return rows


def _chunks(values: set[int], size: int = 500):
    ordered = sorted(values)
    for start in range(0, len(ordered), size):
        yield ordered[start:start + size]
