"""Canonical persistence helpers for provider detections and attributes."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .catalog_provenance import CatalogProvenance, provenance_from_payload
from .catalog_types import CatalogAdapter, CatalogCandidate
from .models.catalogs import (
    CatalogAttribute,
    CatalogDetection,
    CatalogDetectionProvenance,
)


def canonical_detection(
    session: Session,
    adapter: CatalogAdapter,
    candidate: CatalogCandidate,
) -> CatalogDetection:
    key_factory = getattr(adapter, "detection_key", None)
    detection_key = (
        str(key_factory(candidate))
        if callable(key_factory)
        else candidate.detection_key or candidate.source_id
    ).strip()
    if not detection_key:
        raise ValueError(f"{adapter.name} candidate has no stable detection key")
    import json

    payload_json = json.dumps(candidate.payload, sort_keys=True, ensure_ascii=False)
    session.execute(
        sqlite_insert(CatalogDetection)
        .values(
            provider=adapter.name,
            release=adapter.release,
            detection_key=detection_key,
            source_id=candidate.source_id,
            ra_deg=candidate.ra_deg,
            dec_deg=candidate.dec_deg,
            epoch=candidate.epoch,
            payload_json=payload_json,
            normalization_status="pending",
            normalization_error=None,
            normalized_at=None,
            created_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_update(
            index_elements=["provider", "release", "detection_key"],
            set_={
                "source_id": candidate.source_id,
                "ra_deg": candidate.ra_deg,
                "dec_deg": candidate.dec_deg,
                "epoch": candidate.epoch,
                "payload_json": payload_json,
                "normalization_status": "pending",
                "normalization_error": None,
                "normalized_at": None,
            },
        )
    )
    detection = session.scalar(select(CatalogDetection).where(
        CatalogDetection.provider == adapter.name,
        CatalogDetection.release == adapter.release,
        CatalogDetection.detection_key == detection_key,
    ))
    if detection is None:
        raise RuntimeError("failed to create or retrieve canonical catalog detection")
    store_detection_provenance(session, detection, candidate)
    return detection


def store_detection_provenance(
    session: Session,
    detection: CatalogDetection,
    candidate: CatalogCandidate,
) -> None:
    provenance = (
        candidate.provenance
        or provenance_from_payload(candidate.payload)
        or (CatalogProvenance(
            service=detection.provider,
            catalog_id=detection.release.split("@", 1)[0],
        ),)
    )
    for item in provenance:
        key = item.key
        session.execute(
            sqlite_insert(CatalogDetectionProvenance)
            .values(
                detection_id=detection.id,
                provenance_key=key,
                role=item.role,
                service=item.service,
                catalog_id=item.catalog_id,
                table_id=item.table_id,
                row_key=item.row_key,
                identifier_column=item.identifier_column,
                identifier_value=item.identifier_value,
                source_url=item.source_url,
                access_url=item.access_url,
                readme_url=item.readme_url,
                created_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(
                index_elements=["detection_id", "provenance_key"]
            )
        )


def store_catalog_attributes(
    session: Session,
    candidate: CatalogCandidate,
    *,
    run_id: int,
    target_id: int,
    raw_row_id: int,
    provider: str,
) -> None:
    for value in candidate.attributes:
        if value.value_text is None and value.value_float is None:
            continue
        session.add(CatalogAttribute(
            run_id=run_id,
            target_id=target_id,
            raw_row_id=raw_row_id,
            provider=provider,
            source_id=candidate.source_id,
            key=value.key,
            value_text=value.value_text,
            value_float=value.value_float,
            uncertainty=value.uncertainty,
            unit=value.unit,
            quality=value.quality,
            reference=value.reference,
            note=value.note,
        ))


def shared_detection_target_ids(
    session: Session, target_id: int, detection_id: int,
) -> set[int]:
    from .catalog_measurements import current_catalog_detection_target_pairs

    target_ids = {
        current_target_id
        for current_detection_id, current_target_id
        in current_catalog_detection_target_pairs(session, [detection_id])
        if current_detection_id == detection_id
    }
    target_ids.add(target_id)
    return target_ids - {target_id}
