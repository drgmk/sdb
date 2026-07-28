from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .adapters import catalog_source_display_name
from .decisions import DecisionContext
from .dirty import find_target, mark_export_dirty
from .models import (
    CatalogDetection,
    MeasurementAssociationAction,
    MeasurementTargetAssociation,
    NormalizedMeasurement,
    PhotometryOverride,
    RawCatalogRow,
    Target,
    TargetLifecycleAction,
)
from .target_lifecycle import TARGET_ROLES, TARGET_STATES


_REVIEW_STATES = TARGET_STATES - {"superseded"}


def review_photometry_eligibility_decision(
    session_factory: sessionmaker[Session],
    *,
    changes: list[dict[str, object]],
    apply: bool = False,
    actor: str | None = None,
    reason: str | None = None,
    expected_token: str | None = None,
) -> dict[str, object]:
    """Preview or atomically append target/provider/band fit overrides."""
    if not changes:
        raise ValueError("at least one fit-eligibility change is required")
    context = session_factory.begin() if apply else session_factory()
    with context as session:
        normalized: list[dict[str, object]] = []
        seen: set[tuple[int, str, str]] = set()
        for value in changes:
            target_reference = value.get("target")
            if target_reference in {None, ""}:
                raise ValueError("each fit-eligibility change requires a target")
            target = find_target(session, str(target_reference))
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            provider = str(value.get("provider") or "").strip().lower()
            band = str(value.get("band") or "").strip().upper()
            if not provider or not band:
                raise ValueError(
                    "each fit-eligibility change requires provider and band"
                )
            excluded = value.get("excluded")
            if not isinstance(excluded, bool):
                raise ValueError("excluded must be true or false")
            key = (target.id, provider, band)
            if key in seen:
                raise ValueError(
                    f"duplicate fit-eligibility change: {target.sdbid} {provider} {band}"
                )
            seen.add(key)
            latest = session.scalar(
                select(PhotometryOverride)
                .where(
                    PhotometryOverride.target_id == target.id,
                    PhotometryOverride.provider == provider,
                    PhotometryOverride.band == band,
                )
                .order_by(PhotometryOverride.id.desc())
                .limit(1)
            )
            normalized.append({
                "target_id": target.id,
                "sdbid": target.sdbid,
                "provider": provider,
                "band": band,
                "current_override_id": None if latest is None else latest.id,
                "current_excluded": None if latest is None else latest.excluded,
                "desired_excluded": excluded,
                "has_change": latest is None or latest.excluded != excluded,
            })

        normalized.sort(key=lambda row: (
            str(row["sdbid"]), str(row["provider"]), str(row["band"]),
        ))
        token = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        result: dict[str, object] = {
            "mode": "preview",
            "state_token": token,
            "has_changes": any(row["has_change"] for row in normalized),
            "changes": normalized,
            "suggested_reason": _eligibility_suggested_reason(normalized),
            "notes": [
                "fit eligibility is independent of measurement ownership",
                "overrides apply to the measurement origin target, provider, and band",
                "previous overrides remain in the append-only audit history",
            ],
        }
        if not apply:
            return result
        if expected_token is not None and expected_token != token:
            raise RuntimeError(
                "fit eligibility changed after preview; reload and preview again"
            )
        decision = DecisionContext.resolve(
            actor=actor,
            reason=reason,
            suggested_reason=str(result["suggested_reason"]),
        )
        applied_ids = []
        for value in normalized:
            if not value["has_change"]:
                continue
            override = PhotometryOverride(
                target_id=int(value["target_id"]),
                provider=str(value["provider"]),
                band=str(value["band"]),
                excluded=bool(value["desired_excluded"]),
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(override)
            session.flush()
            applied_ids.append(override.id)
            mark_export_dirty(
                session,
                int(value["target_id"]),
                source_type="review_photometry_eligibility",
                source_id=override.id,
                reason="reviewed photometry fit eligibility changed",
            )
        return {
            **result,
            "mode": "applied",
            "applied": {
                "overrides_added": len(applied_ids),
                "override_ids": applied_ids,
            },
        }


def review_target_lifecycle_decision(
    session_factory: sessionmaker[Session],
    *,
    target_reference: str | int,
    role: str,
    state: str,
    apply: bool = False,
    actor: str | None = None,
    reason: str | None = None,
    expected_token: str | None = None,
) -> dict[str, object]:
    """Preview or atomically apply the fitted-model/measurement-scope role."""
    role = role.strip().lower()
    state = state.strip().lower()
    if role not in TARGET_ROLES:
        raise ValueError(f"role must be one of {sorted(TARGET_ROLES)}")
    if state not in _REVIEW_STATES:
        raise ValueError(f"state must be one of {sorted(_REVIEW_STATES)}")
    context = session_factory.begin() if apply else session_factory()
    with context as session:
        target = find_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        latest = session.scalar(
            select(TargetLifecycleAction)
            .where(TargetLifecycleAction.target_id == target.id)
            .order_by(TargetLifecycleAction.id.desc())
            .limit(1)
        )
        current_role = "unspecified" if latest is None else latest.role
        current_state = "active" if latest is None else latest.state
        reconciliation = []
        if role == "physical":
            scope_rows = list(session.scalars(
                select(MeasurementTargetAssociation)
                .where(
                    MeasurementTargetAssociation.target_id == target.id,
                    MeasurementTargetAssociation.role == "composite_scope",
                )
                .order_by(MeasurementTargetAssociation.measurement_id)
            ))
            existing_contributors = set(session.scalars(
                select(MeasurementTargetAssociation.measurement_id)
                .where(
                    MeasurementTargetAssociation.target_id == target.id,
                    MeasurementTargetAssociation.role == "contributor",
                )
            ))
            reconciliation = [{
                "association_id": row.id,
                "measurement_id": row.measurement_id,
                "target_id": target.id,
                "sdbid": target.sdbid,
                "from_role": "composite_scope",
                "to_role": "contributor",
                "add_contributor": row.measurement_id not in existing_contributors,
            } for row in scope_rows]
        token = hashlib.sha256(json.dumps({
            "target_id": target.id,
            "lifecycle_action_id": None if latest is None else latest.id,
            "current_role": current_role,
            "current_state": current_state,
            "associations": [
                [row["association_id"], row["measurement_id"], row["from_role"]]
                for row in reconciliation
            ],
        }, sort_keys=True).encode("utf-8")).hexdigest()
        result = {
            "mode": "preview",
            "state_token": token,
            "target": _target_row(target),
            "current": {"role": current_role, "state": current_state},
            "desired": {"role": role, "state": state},
            "has_changes": (
                (role, state) != (current_role, current_state)
                or bool(reconciliation)
            ),
            "interpretation": _lifecycle_interpretation(role),
            "assignment_reconciliation": reconciliation,
            "suggested_reason": (
                f"Set {target.sdbid} modelling role to {role} with state {state}"
            ),
        }
        if not apply:
            return result
        if expected_token is not None and expected_token != token:
            raise RuntimeError(
                "target lifecycle changed after preview; reload and preview again"
            )
        decision = DecisionContext.resolve(
            actor=actor,
            reason=reason,
            suggested_reason=str(result["suggested_reason"]),
        )
        lifecycle_count = 0
        action_id = None
        if (role, state) != (current_role, current_state):
            action = TargetLifecycleAction(
                target_id=target.id,
                role=role,
                state=state,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(action)
            session.flush()
            lifecycle_count = 1
            action_id = action.id
            mark_export_dirty(
                session,
                target.id,
                source_type="review_target_lifecycle",
                source_id=action.id,
                reason="reviewed fitted-model/measurement-scope role changed",
            )
        removed, added = _apply_lifecycle_assignment_reconciliation(
            session,
            reconciliation,
            actor=decision.actor,
            reason=decision.reason,
        )
        return {
            **result,
            "mode": "applied",
            "applied": {
                "lifecycle_actions": lifecycle_count,
                "action_id": action_id,
                "assignments_removed": removed,
                "assignments_added": added,
            },
        }


def _lifecycle_interpretation(role: str) -> dict[str, object]:
    if role == "physical":
        return {
            "model_target": True,
            "summary": "fit one photospheric model for this target",
            "multiplicity": (
                "known WDS/CCDM multiplicity is retained as context and does not "
                "prevent combined-light modelling"
            ),
        }
    if role == "composite":
        return {
            "model_target": False,
            "summary": "use this target only as a measurement scope",
            "multiplicity": (
                "photometry needs one or more separately imported physical "
                "contributors before joint fitting is resolved"
            ),
        }
    return {
        "model_target": True,
        "summary": "retain the current undecided default temporarily",
        "multiplicity": "review is still required before joint fitting",
    }


def _apply_lifecycle_assignment_reconciliation(
    session: Session,
    rows: list[dict[str, object]],
    *,
    actor: str,
    reason: str,
) -> tuple[int, int]:
    removed = added = 0
    for value in rows:
        association = session.get(
            MeasurementTargetAssociation, value["association_id"],
        )
        if association is None or association.role != value["from_role"]:
            raise RuntimeError(
                "measurement ownership changed after lifecycle preview; reload and preview again"
            )
        session.add(MeasurementAssociationAction(
            measurement_id=association.measurement_id,
            target_id=association.target_id,
            action="unassign",
            role=association.role,
            method="lifecycle_role_reconciliation",
            weight=association.weight,
            actor=actor,
            reason=reason,
        ))
        session.delete(association)
        removed += 1
        if value["add_contributor"]:
            session.add(MeasurementTargetAssociation(
                measurement_id=value["measurement_id"],
                target_id=value["target_id"],
                role="contributor",
                method="lifecycle_role_reconciliation",
                note=reason,
            ))
            session.add(MeasurementAssociationAction(
                measurement_id=value["measurement_id"],
                target_id=value["target_id"],
                action="assign",
                role="contributor",
                method="lifecycle_role_reconciliation",
                actor=actor,
                reason=reason,
            ))
            added += 1
        measurement = session.get(
            NormalizedMeasurement, value["measurement_id"],
        )
        for dirty_target_id in {
            value["target_id"],
            None if measurement is None else measurement.target_id,
        } - {None}:
            mark_export_dirty(
                session,
                dirty_target_id,
                source_type="lifecycle_role_reconciliation",
                source_id=value["measurement_id"],
                reason="measurement ownership reconciled with target role",
            )
    return removed, added


def review_detection_decision(
    session_factory: sessionmaker[Session],
    *,
    detection_id: int,
    scope_target_reference: str | int,
    contributor_references: list[str | int] | tuple[str | int, ...] = (),
    include_composite_scope: bool,
    measurement_ids: list[int] | tuple[int, ...] | None = None,
    target_role: str | None = None,
    target_state: str | None = None,
    apply: bool = False,
    actor: str | None = None,
    reason: str | None = None,
    expected_token: str | None = None,
) -> dict[str, object]:
    """Preview or atomically apply one reviewed catalog-detection decision."""
    if (target_role is None) != (target_state is None):
        raise ValueError("target_role and target_state must be supplied together")
    if target_role is not None and target_role not in TARGET_ROLES:
        raise ValueError(f"target_role must be one of {sorted(TARGET_ROLES)}")
    if target_state is not None and target_state not in _REVIEW_STATES:
        raise ValueError(f"target_state must be one of {sorted(_REVIEW_STATES)}")

    context = session_factory.begin() if apply else session_factory()
    with context as session:
        snapshot = _decision_snapshot(
            session,
            detection_id=detection_id,
            scope_target_reference=scope_target_reference,
            contributor_references=contributor_references,
            include_composite_scope=include_composite_scope,
            measurement_ids=measurement_ids,
            target_role=target_role,
            target_state=target_state,
        )
        if not apply:
            return snapshot
        if expected_token is not None and expected_token != snapshot["state_token"]:
            raise RuntimeError(
                "review state changed after preview; reload and preview the decision again"
            )
        decision = DecisionContext.resolve(
            actor=actor,
            reason=reason,
            suggested_reason=str(snapshot["suggested_reason"]),
        )
        lifecycle_applied = _apply_lifecycle(
            session,
            snapshot,
            actor=decision.actor,
            reason=decision.reason,
        )
        removed = _remove_assignments(
            session,
            snapshot,
            actor=decision.actor,
            reason=decision.reason,
        )
        added = _add_assignments(
            session,
            snapshot,
            actor=decision.actor,
            reason=decision.reason,
        )
        dirty_target_ids = {
            snapshot["scope_target"]["target_id"],
            *(row["origin_target_id"] for row in snapshot["measurements"]),
            *(row["target_id"] for row in snapshot["desired_targets"]),
            *(row["target_id"] for row in snapshot["remove_assignments"]),
        }
        for target_id in dirty_target_ids:
            mark_export_dirty(
                session,
                int(target_id),
                source_type="review_detection_decision",
                source_id=detection_id,
                reason="reviewed catalog-detection ownership changed",
            )
        session.flush()
        return {
            **snapshot,
            "mode": "applied",
            "applied": {
                "lifecycle_actions": lifecycle_applied,
                "assignments_added": added,
                "assignments_removed": removed,
            },
        }


def _decision_snapshot(
    session: Session,
    *,
    detection_id: int,
    scope_target_reference: str | int,
    contributor_references: list[str | int] | tuple[str | int, ...],
    include_composite_scope: bool,
    measurement_ids: list[int] | tuple[int, ...] | None,
    target_role: str | None,
    target_state: str | None,
) -> dict[str, object]:
    detection = session.get(CatalogDetection, detection_id)
    if detection is None:
        raise KeyError(f"catalog detection not found: {detection_id}")
    scope_target = find_target(session, scope_target_reference)
    if scope_target is None:
        raise KeyError(f"target not found: {scope_target_reference}")
    contributors = []
    seen = set()
    for reference in contributor_references:
        target = find_target(session, reference)
        if target is None:
            raise KeyError(f"contributor target not found: {reference}")
        if target.id not in seen:
            contributors.append(target)
            seen.add(target.id)

    measurements = list(session.scalars(
        select(NormalizedMeasurement)
        .where(NormalizedMeasurement.detection_id == detection.id)
        .order_by(NormalizedMeasurement.band, NormalizedMeasurement.id)
    ))
    if measurement_ids is not None:
        selected = {int(value) for value in measurement_ids}
        available = {value.id for value in measurements}
        missing = selected - available
        if missing:
            raise ValueError(
                f"measurements do not belong to detection {detection.id}: {sorted(missing)}"
            )
        measurements = [value for value in measurements if value.id in selected]
    if not measurements:
        raise ValueError("select at least one measurement from the detection")
    raw = (
        None
        if measurements[0].raw_row_id is None
        else session.get(RawCatalogRow, measurements[0].raw_row_id)
    )
    payload = None
    if raw is not None:
        try:
            parsed_payload = json.loads(raw.payload_json)
        except (TypeError, json.JSONDecodeError):
            parsed_payload = None
        if isinstance(parsed_payload, dict):
            payload = parsed_payload
    source_display_name = catalog_source_display_name(
        detection.provider,
        detection.source_id,
        payload,
    )
    selected_ids = {value.id for value in measurements}
    current = list(session.scalars(
        select(MeasurementTargetAssociation)
        .where(MeasurementTargetAssociation.measurement_id.in_(selected_ids))
        .order_by(
            MeasurementTargetAssociation.measurement_id,
            MeasurementTargetAssociation.role,
            MeasurementTargetAssociation.target_id,
        )
    ))
    target_ids = {row.target_id for row in current} | seen | {scope_target.id}
    targets = {
        value.id: value for value in session.scalars(
            select(Target).where(Target.id.in_(target_ids))
        )
    }
    desired_pairs = {
        (measurement.id, target.id, "contributor")
        for measurement in measurements for target in contributors
    }
    if include_composite_scope:
        desired_pairs.update(
            (measurement.id, scope_target.id, "composite_scope")
            for measurement in measurements
        )
    current_pairs = {
        (row.measurement_id, row.target_id, row.role) for row in current
    }
    add_pairs = sorted(desired_pairs - current_pairs)
    remove_rows = [
        row for row in current
        if (row.measurement_id, row.target_id, row.role) not in desired_pairs
    ]
    latest_lifecycle = session.scalar(
        select(TargetLifecycleAction)
        .where(TargetLifecycleAction.target_id == scope_target.id)
        .order_by(TargetLifecycleAction.id.desc())
        .limit(1)
    )
    current_role = "unspecified" if latest_lifecycle is None else latest_lifecycle.role
    current_state = "active" if latest_lifecycle is None else latest_lifecycle.state
    lifecycle_change = None
    if target_role is not None and (target_role, target_state) != (current_role, current_state):
        lifecycle_change = {
            "target_id": scope_target.id,
            "sdbid": scope_target.sdbid,
            "from_role": current_role,
            "from_state": current_state,
            "to_role": target_role,
            "to_state": target_state,
        }
    token_payload = {
        "detection_id": detection.id,
        "measurement_ids": sorted(selected_ids),
        "associations": [
            [row.id, row.measurement_id, row.target_id, row.role]
            for row in current
        ],
        "lifecycle_action_id": None if latest_lifecycle is None else latest_lifecycle.id,
    }
    add_description = ", ".join(
        f"{row['role']} for {row['sdbid']}" for row in [
            {
                "measurement_id": measurement_id,
                **_target_row(targets[target_id]),
                "role": assignment_role,
            }
            for measurement_id, target_id, assignment_role in add_pairs
        ]
    )
    remove_description = ", ".join(
        f"{row.role} for {targets[row.target_id].sdbid}" for row in remove_rows
    )
    reason_parts = [
        f"Reviewed {detection.provider} source {source_display_name}",
        *([f"assigned {add_description}"] if add_description else []),
        *([f"removed {remove_description}"] if remove_description else []),
        *(
            [f"set {scope_target.sdbid} role to {target_role}"]
            if lifecycle_change else []
        ),
    ]
    return {
        "mode": "preview",
        "state_token": hashlib.sha256(
            json.dumps(token_payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "detection": {
            "detection_id": detection.id,
            "provider": detection.provider,
            "release": detection.release,
            "source_id": detection.source_id,
            "source_display_name": source_display_name,
            "ra_deg": detection.ra_deg,
            "dec_deg": detection.dec_deg,
            "epoch": detection.epoch,
        },
        "scope_target": _target_row(scope_target),
        "measurements": [{
            "measurement_id": value.id,
            "origin_target_id": value.target_id,
            "band": value.band,
            "value": value.value,
            "error": value.error,
            "unit": value.unit,
            "excluded": value.excluded,
            "exclusion_reason": value.exclusion_reason,
            "resolution_major_arcsec": value.resolution_major_arcsec,
            "resolution_minor_arcsec": value.resolution_minor_arcsec,
        } for value in measurements],
        "desired_targets": [
            {**_target_row(target), "role": "contributor"}
            for target in contributors
        ] + ([{
            **_target_row(scope_target), "role": "composite_scope",
        }] if include_composite_scope else []),
        "current_assignments": [
            _association_row(row, targets[row.target_id]) for row in current
        ],
        "add_assignments": [
            {
                "measurement_id": measurement_id,
                **_target_row(targets[target_id]),
                "role": role,
            }
            for measurement_id, target_id, role in add_pairs
        ],
        "remove_assignments": [
            _association_row(row, targets[row.target_id]) for row in remove_rows
        ],
        "lifecycle_change": lifecycle_change,
        "has_changes": bool(add_pairs or remove_rows or lifecycle_change),
        "suggested_reason": "; ".join(reason_parts),
        "notes": [
            "selected bands from one canonical catalog detection are reviewed together",
            "provider exclusions are preserved independently of ownership assignments",
            "apply replaces current assignments for only the selected measurements",
        ],
    }


def _eligibility_suggested_reason(
    changes: list[dict[str, object]],
) -> str:
    descriptions = [
        (
            f"{'Excluded' if row['desired_excluded'] else 'Included'} "
            f"{row['sdbid']} {row['provider']} {row['band']}"
        )
        for row in changes
        if row["has_change"]
    ]
    return "; ".join(descriptions) or "Confirmed current photometry fit eligibility"


def _apply_lifecycle(
    session: Session, snapshot: dict[str, object], *, actor: str, reason: str,
) -> int:
    change = snapshot["lifecycle_change"]
    if change is None:
        return 0
    session.add(TargetLifecycleAction(
        target_id=change["target_id"],
        role=change["to_role"],
        state=change["to_state"],
        actor=actor,
        reason=reason,
    ))
    return 1


def _remove_assignments(
    session: Session, snapshot: dict[str, object], *, actor: str, reason: str,
) -> int:
    count = 0
    for value in snapshot["remove_assignments"]:
        row = session.get(MeasurementTargetAssociation, value["association_id"])
        if row is None:
            raise RuntimeError("review state changed while applying removed assignments")
        session.add(MeasurementAssociationAction(
            measurement_id=row.measurement_id,
            target_id=row.target_id,
            action="unassign",
            role=row.role,
            method="review_ui_detection",
            weight=row.weight,
            actor=actor,
            reason=reason,
        ))
        session.delete(row)
        count += 1
    return count


def _add_assignments(
    session: Session, snapshot: dict[str, object], *, actor: str, reason: str,
) -> int:
    count = 0
    for value in snapshot["add_assignments"]:
        session.add(MeasurementTargetAssociation(
            measurement_id=value["measurement_id"],
            target_id=value["target_id"],
            role=value["role"],
            method="review_ui_detection",
            note=reason,
        ))
        session.add(MeasurementAssociationAction(
            measurement_id=value["measurement_id"],
            target_id=value["target_id"],
            action="assign",
            role=value["role"],
            method="review_ui_detection",
            actor=actor,
            reason=reason,
        ))
        count += 1
    return count


def _target_row(target: Target) -> dict[str, object]:
    return {"target_id": target.id, "sdbid": target.sdbid}


def _association_row(
    association: MeasurementTargetAssociation, target: Target,
) -> dict[str, object]:
    return {
        "association_id": association.id,
        "measurement_id": association.measurement_id,
        "target_id": association.target_id,
        "sdbid": target.sdbid,
        "role": association.role,
        "method": association.method,
        "weight": association.weight,
    }
