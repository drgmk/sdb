from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .dirty import mark_export_dirty
from .catalog_measurements import current_measurement_encounters
from .models import (
    CatalogRun, ExternalIdentifier, MeasurementAssociationAction,
    MeasurementTargetAssociation, NormalizedMeasurement,
    PhotometryOverride, RawCatalogRow, Target,
)
from .service import normalize_identifier


def _target(session: Session, reference: str | int) -> Target | None:
    if isinstance(reference, int) or str(reference).isdigit():
        return session.get(Target, int(reference))
    target = session.scalar(select(Target).where(Target.sdbid == str(reference)))
    if target is not None:
        return target
    identifier = session.scalar(
        select(ExternalIdentifier).where(
            ExternalIdentifier.normalized_value == normalize_identifier(str(reference))
        ).limit(1)
    )
    return None if identifier is None else session.get(Target, identifier.target_id)


def set_photometry_override(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    provider: str,
    band: str,
    excluded: bool,
    actor: str,
    reason: str,
) -> PhotometryOverride:
    if not provider.strip() or not band.strip():
        raise ValueError("provider and band are required")
    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required")
    with session_factory.begin() as session:
        target = _target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        value = PhotometryOverride(
            target_id=target.id,
            provider=provider.strip().lower(),
            band=band.strip().upper(),
            excluded=excluded,
            actor=actor.strip(),
            reason=reason.strip(),
        )
        session.add(value)
        session.flush()
        mark_export_dirty(
            session,
            target.id,
            source_type="photometry_override",
            source_id=value.id,
            reason=f"{value.provider} {value.band} photometry override",
        )
    return value


def list_photometry_overrides(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
) -> list[PhotometryOverride]:
    with session_factory() as session:
        target = _target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        return list(
            session.scalars(
                select(PhotometryOverride)
                .where(PhotometryOverride.target_id == target.id)
                .order_by(PhotometryOverride.id)
            )
        )


MEASUREMENT_TARGET_ROLES = {"contributor", "composite_scope"}


@dataclass(frozen=True)
class PhotometryReviewRow:
    target_id: int
    provider: str
    source_id: str
    band: str | None
    measurement_id: int | None
    raw_row_id: int | None
    value: float | None
    unit: str | None
    excluded: bool | None
    association_scope: str | None
    blend_status: str | None


def assign_measurement_target(
    session_factory: sessionmaker[Session],
    measurement_id: int,
    target_reference: str | int,
    *,
    role: str = "contributor",
    method: str = "manual",
    weight: float | None = None,
    actor: str,
    reason: str,
) -> MeasurementTargetAssociation:
    role = role.strip().lower()
    method = method.strip().lower()
    if role not in MEASUREMENT_TARGET_ROLES:
        raise ValueError(f"role must be one of {sorted(MEASUREMENT_TARGET_ROLES)}")
    if not method or not actor.strip() or not reason.strip():
        raise ValueError("method, actor, and reason are required")
    if weight is not None and weight < 0:
        raise ValueError("weight must be non-negative")
    with session_factory.begin() as session:
        measurement = session.get(NormalizedMeasurement, measurement_id)
        if measurement is None:
            raise KeyError(f"measurement not found: {measurement_id}")
        target = _target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        association = session.scalar(select(MeasurementTargetAssociation).where(
            MeasurementTargetAssociation.measurement_id == measurement.id,
            MeasurementTargetAssociation.target_id == target.id,
            MeasurementTargetAssociation.role == role,
        ))
        if association is None:
            association = MeasurementTargetAssociation(
                measurement_id=measurement.id,
                target_id=target.id,
                role=role,
                method=method,
                weight=weight,
                note=reason.strip(),
            )
            session.add(association)
        else:
            association.method = method
            association.weight = weight
            association.note = reason.strip()
        action = MeasurementAssociationAction(
            measurement_id=measurement.id,
            target_id=target.id,
            action="assign",
            role=role,
            method=method,
            weight=weight,
            actor=actor.strip(),
            reason=reason.strip(),
        )
        session.add(action)
        session.flush()
        for dirty_target_id in {measurement.target_id, target.id}:
            mark_export_dirty(
                session,
                dirty_target_id,
                source_type="measurement_assignment",
                source_id=action.id,
                reason="measurement contributor assignment changed",
            )
        return association


def unassign_measurement_target(
    session_factory: sessionmaker[Session],
    measurement_id: int,
    target_reference: str | int,
    *,
    role: str = "contributor",
    actor: str,
    reason: str,
) -> MeasurementAssociationAction:
    role = role.strip().lower()
    if role not in MEASUREMENT_TARGET_ROLES:
        raise ValueError(f"role must be one of {sorted(MEASUREMENT_TARGET_ROLES)}")
    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required")
    with session_factory.begin() as session:
        measurement = session.get(NormalizedMeasurement, measurement_id)
        if measurement is None:
            raise KeyError(f"measurement not found: {measurement_id}")
        target = _target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        association = session.scalar(select(MeasurementTargetAssociation).where(
            MeasurementTargetAssociation.measurement_id == measurement.id,
            MeasurementTargetAssociation.target_id == target.id,
            MeasurementTargetAssociation.role == role,
        ))
        if association is None:
            raise KeyError("current measurement assignment not found")
        action = MeasurementAssociationAction(
            measurement_id=measurement.id,
            target_id=target.id,
            action="unassign",
            role=association.role,
            method=association.method,
            weight=association.weight,
            actor=actor.strip(),
            reason=reason.strip(),
        )
        session.add(action)
        session.delete(association)
        session.flush()
        for dirty_target_id in {measurement.target_id, target.id}:
            mark_export_dirty(
                session,
                dirty_target_id,
                source_type="measurement_assignment",
                source_id=action.id,
                reason="measurement contributor assignment changed",
            )
        return action


def list_measurement_target_assignments(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
) -> list[dict[str, object]]:
    with session_factory() as session:
        target = _target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        rows = session.execute(
            select(MeasurementTargetAssociation, NormalizedMeasurement)
            .join(
                NormalizedMeasurement,
                NormalizedMeasurement.id == MeasurementTargetAssociation.measurement_id,
            )
            .where(MeasurementTargetAssociation.target_id == target.id)
            .order_by(NormalizedMeasurement.provider, NormalizedMeasurement.band, MeasurementTargetAssociation.id)
        )
        return [{
            "association_id": association.id,
            "measurement_id": measurement.id,
            "origin_target_id": measurement.target_id,
            "target_id": target.id,
            "sdbid": target.sdbid,
            "provider": measurement.provider,
            "source_id": measurement.source_id,
            "band": measurement.band,
            "role": association.role,
            "method": association.method,
            "weight": association.weight,
            "note": association.note,
        } for association, measurement in rows]


def list_measurement_assignment_history(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
) -> list[MeasurementAssociationAction]:
    with session_factory() as session:
        target = _target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        return list(session.scalars(select(MeasurementAssociationAction).where(
            MeasurementAssociationAction.target_id == target.id
        ).order_by(MeasurementAssociationAction.id)))


def review_photometry_associations(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
) -> list[PhotometryReviewRow]:
    with session_factory() as session:
        target = _target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        rows: list[PhotometryReviewRow] = []
        encounters = current_measurement_encounters(
            session, [target.id], require_match=False,
        )
        for encounter in encounters:
            measurement = encounter.measurement
            raw_row_id = encounter.raw_row.id
            rows.append(PhotometryReviewRow(
                target_id=target.id,
                provider=measurement.provider,
                source_id=measurement.source_id,
                band=measurement.band,
                measurement_id=measurement.id,
                raw_row_id=raw_row_id,
                value=measurement.value,
                unit=measurement.unit,
                excluded=measurement.excluded,
                association_scope=measurement.association_scope,
                blend_status=measurement.blend_status,
            ))
        raw_rows = session.scalars(
            select(RawCatalogRow)
            .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
            .where(CatalogRun.target_id == target.id)
            .where(CatalogRun.is_current.is_(True))
            .where(RawCatalogRow.accepted.is_(False))
            .order_by(CatalogRun.provider, RawCatalogRow.source_id, RawCatalogRow.id)
        )
        for raw in raw_rows:
            run = session.get(CatalogRun, raw.run_id)
            rows.append(PhotometryReviewRow(
                target_id=target.id,
                provider="" if run is None else run.provider,
                source_id=raw.source_id,
                band=None,
                measurement_id=None,
                raw_row_id=raw.id,
                value=None,
                unit=None,
                excluded=None,
                association_scope=None,
                blend_status=None,
            ))
        return rows


def _review_photometry_associations_many(
    session_factory: sessionmaker[Session],
    targets: list[Target],
) -> dict[int, list[PhotometryReviewRow]]:
    """Load review rows for several targets without per-target SQL queries."""
    if not targets:
        return {}
    target_ids = [target.id for target in targets]
    rows_by_target: dict[int, list[PhotometryReviewRow]] = {
        target_id: [] for target_id in target_ids
    }
    with session_factory() as session:
        for target_id_chunk in _chunks(target_ids):
            encounters = current_measurement_encounters(
                session, target_id_chunk, require_match=False,
            )
            for encounter in encounters:
                measurement = encounter.measurement
                target_id = encounter.target_id
                raw_row_id = encounter.raw_row.id
                rows_by_target[target_id].append(PhotometryReviewRow(
                    target_id=target_id,
                    provider=measurement.provider,
                    source_id=measurement.source_id,
                    band=measurement.band,
                    measurement_id=measurement.id,
                    raw_row_id=raw_row_id,
                    value=measurement.value,
                    unit=measurement.unit,
                    excluded=measurement.excluded,
                    association_scope=measurement.association_scope,
                    blend_status=measurement.blend_status,
                ))

        for target_id_chunk in _chunks(target_ids):
            raw_rows = session.execute(
                select(RawCatalogRow, CatalogRun.provider, CatalogRun.target_id)
                .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
                .where(CatalogRun.target_id.in_(target_id_chunk))
                .where(CatalogRun.is_current.is_(True))
                .where(RawCatalogRow.accepted.is_(False))
                .order_by(
                    CatalogRun.target_id,
                    CatalogRun.provider,
                    RawCatalogRow.source_id,
                    RawCatalogRow.id,
                )
            )
            for raw, run_provider, target_id in raw_rows:
                rows_by_target[target_id].append(PhotometryReviewRow(
                    target_id=target_id,
                    provider=run_provider,
                    source_id=raw.source_id,
                    band=None,
                    measurement_id=None,
                    raw_row_id=raw.id,
                    value=None,
                    unit=None,
                    excluded=None,
                    association_scope=None,
                    blend_status=None,
                ))
    return rows_by_target


def _chunks(values: list[int], size: int = 500) -> tuple[list[int], ...]:
    return tuple(values[index:index + size] for index in range(0, len(values), size))


def photometry_review_queue(
    session_factory: sessionmaker[Session],
    target_references: list[str | int],
    *,
    provider: str | None = None,
) -> list[dict[str, object]]:
    provider_value = None if provider is None else provider.strip().lower()
    from .hierarchy import HierarchyService

    hierarchy_rows = HierarchyService(session_factory).photometry_review(
        target_references, provider=provider_value
    )
    hierarchy_by_sdbid = {str(row["sdbid"]): row for row in hierarchy_rows}
    with session_factory() as session:
        targets: list[Target] = []
        for reference in target_references:
            target = _target(session, reference)
            if target is None:
                raise KeyError(f"target not found: {reference}")
            targets.append(target)
    review_rows_by_target = _review_photometry_associations_many(session_factory, targets)

    rows: list[dict[str, object]] = []
    for target in targets:
        sdbid = target.sdbid
        hierarchy = hierarchy_by_sdbid.get(sdbid, {})
        band_context = {
            (str(band.get("provider")), str(band.get("band"))): band
            for band in hierarchy.get("bands", [])
        }
        review_rows = review_rows_by_target[target.id]
        if provider_value is not None:
            review_rows = [row for row in review_rows if row.provider == provider_value]
        target_rows = []
        for row in review_rows:
            context = band_context.get((row.provider, str(row.band))) if row.band else None
            signal, priority, action = _photometry_queue_signal(row, context)
            if priority == "none":
                continue
            target_rows.append({
                "sdbid": sdbid,
                "target_id": row.target_id,
                "provider": row.provider,
                "source_id": row.source_id,
                "band": row.band,
                "measurement_id": row.measurement_id,
                "raw_row_id": row.raw_row_id,
                "signal": signal,
                "priority": priority,
                "predicted_scope": None if context is None else context.get("predicted_association_scope"),
                "predicted_blend_status": None if context is None else context.get("predicted_scope_blend_status"),
                "stored_scope": row.association_scope,
                "stored_blend_status": row.blend_status,
                "action": action,
            })
        if not target_rows:
            target_rows.append({
                "sdbid": sdbid,
                "target_id": None,
                "provider": provider_value,
                "source_id": None,
                "band": None,
                "measurement_id": None,
                "raw_row_id": None,
                "signal": "no photometry review item",
                "priority": "none",
                "predicted_scope": None,
                "predicted_blend_status": None,
                "stored_scope": None,
                "stored_blend_status": None,
                "action": "none",
            })
        rows.extend(target_rows)
    return sorted(rows, key=lambda row: (
        -_queue_priority_rank(str(row["priority"])),
        str(row["sdbid"]),
        str(row.get("provider") or ""),
        str(row.get("band") or ""),
        str(row.get("source_id") or ""),
    ))


def _photometry_queue_signal(
    row: PhotometryReviewRow,
    context: dict[str, object] | None,
) -> tuple[str, str, str]:
    if row.measurement_id is None and row.raw_row_id is not None:
        return "unaccepted catalog neighbour", "medium", "review; exclude the band if it is not this target's light"
    if row.association_scope == "shared" or row.blend_status == "duplicate_source":
        return "shared catalog source", "high", "inspect shared-source export exclusion"
    if context is not None:
        predicted_scope = str(context.get("predicted_association_scope") or "")
        predicted_blend = str(context.get("predicted_scope_blend_status") or "")
        if predicted_scope in {"blended", "system", "ambiguous"}:
            return f"predicted {predicted_scope}", "high", "assign contributing targets after review"
        if predicted_blend == "likely_blended_at_catalog_resolution":
            return "likely blended at catalog resolution", "high", "assign contributing targets after review"
    return "clean automatic association", "none", "none"


def _queue_priority_rank(priority: str) -> int:
    return {"none": 0, "low": 1, "medium": 2, "high": 3}.get(priority, 0)
