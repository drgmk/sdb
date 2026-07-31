"""Normalize canonical catalog detections into provider measurements."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from .catalog_ingestion import store_detection_provenance
from .catalog_provenance import provenance_from_payload
from .catalog_resolution import default_resolution
from .catalog_types import (
    CatalogAdapter,
    CatalogCandidate,
    DetectionNormalizationItem,
    DetectionNormalizationSummary,
    MeasurementValue,
)
from .models import CatalogDetection, CatalogRun, NormalizedMeasurement, RawCatalogRow
from .photometry_semantics import validate_photometry_semantics


LOGGER = logging.getLogger(__name__)


class CatalogNormalizationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapters: Mapping[str, CatalogAdapter],
    ):
        self.sessions = session_factory
        self.adapters = dict(adapters)

    def normalize_detections(
        self, detection_ids: Iterable[int],
    ) -> DetectionNormalizationSummary:
        """Normalize stored payloads without repeating provider queries."""
        ids = tuple(dict.fromkeys(int(value) for value in detection_ids))
        if not ids:
            return _summary([])
        items = []
        with self.sessions() as session, session.begin():
            detections = list(session.scalars(
                select(CatalogDetection)
                .where(CatalogDetection.id.in_(ids))
                .order_by(CatalogDetection.id)
            ))
            for detection in detections:
                adapter = self.adapters.get(detection.provider)
                if adapter is None:
                    items.append(DetectionNormalizationItem(
                        detection.id, detection.provider, detection.source_id,
                        "failed", 0,
                        f"catalog adapter is unavailable: {detection.provider}",
                    ))
                    continue
                encounter = session.execute(
                    select(RawCatalogRow, CatalogRun)
                    .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
                    .where(RawCatalogRow.detection_id == detection.id)
                    .order_by(RawCatalogRow.id)
                    .limit(1)
                ).first()
                if encounter is None:
                    detection.normalization_status = "failed"
                    detection.normalization_error = "detection has no query encounter"
                    detection.normalized_at = datetime.now(timezone.utc)
                    items.append(DetectionNormalizationItem(
                        detection.id, detection.provider, detection.source_id,
                        "failed", 0, detection.normalization_error,
                    ))
                    continue
                raw, run = encounter
                try:
                    candidate = candidate_from_payload(
                        adapter, json.loads(detection.payload_json)
                    )
                    store_detection_provenance(session, detection, candidate)
                except Exception as error:
                    detection.normalization_status = "failed"
                    detection.normalization_error = str(error)
                    detection.normalized_at = datetime.now(timezone.utc)
                    items.append(DetectionNormalizationItem(
                        detection.id, detection.provider, detection.source_id,
                        "failed", 0, str(error),
                    ))
                    continue
                items.append(normalize_detection(
                    session,
                    adapter=adapter,
                    candidate=candidate,
                    detection=detection,
                    run_id=run.id,
                    target_id=run.target_id,
                    raw_row_id=raw.id,
                    strict=False,
                ))
        return _summary(items)


def candidate_from_payload(
    adapter: CatalogAdapter, payload: dict[str, object],
) -> CatalogCandidate:
    if hasattr(adapter, "candidate_from_payload"):
        candidate = adapter.candidate_from_payload(payload)
    elif hasattr(adapter, "parse_row"):
        candidate = adapter.parse_row(payload)
    else:
        raise ValueError(
            f"{adapter.name} adapter cannot reconstruct stored candidates"
        )
    if candidate.provenance:
        return candidate
    return CatalogCandidate(
        source_id=candidate.source_id,
        ra_deg=candidate.ra_deg,
        dec_deg=candidate.dec_deg,
        epoch=candidate.epoch,
        payload=candidate.payload,
        measurements=candidate.measurements,
        attributes=candidate.attributes,
        detection_key=candidate.detection_key,
        provenance=provenance_from_payload(payload),
    )


def normalize_detection(
    session: Session,
    *,
    adapter: CatalogAdapter,
    candidate: CatalogCandidate,
    detection: CatalogDetection,
    run_id: int,
    target_id: int,
    raw_row_id: int,
    strict: bool,
) -> DetectionNormalizationItem:
    try:
        count = 0
        for value_index, value in enumerate(adapter.normalize(candidate)):
            canonical_measurement(
                session,
                adapter=adapter,
                candidate=candidate,
                detection=detection,
                value=value,
                value_index=value_index,
                run_id=run_id,
                target_id=target_id,
                raw_row_id=raw_row_id,
            )
            count += 1
    except Exception as error:
        if strict:
            raise
        LOGGER.warning(
            "could not normalize unaccepted %s detection %s",
            adapter.name, candidate.source_id, exc_info=True,
        )
        detection.normalization_status = "failed"
        detection.normalization_error = str(error)
        detection.normalized_at = datetime.now(timezone.utc)
        return DetectionNormalizationItem(
            detection.id, detection.provider, detection.source_id,
            "failed", 0, str(error),
        )
    detection.normalization_status = "completed" if count else "no_measurements"
    detection.normalization_error = None
    detection.normalized_at = datetime.now(timezone.utc)
    return DetectionNormalizationItem(
        detection.id, detection.provider, detection.source_id,
        detection.normalization_status, count,
    )


def canonical_measurement(
    session: Session,
    *,
    adapter: CatalogAdapter,
    candidate: CatalogCandidate,
    detection: CatalogDetection,
    value: MeasurementValue,
    value_index: int,
    run_id: int,
    target_id: int,
    raw_row_id: int,
) -> NormalizedMeasurement:
    ownership_scope, blend_state = validate_photometry_semantics(
        value.ownership_scope, value.blend_state
    )
    key_factory = getattr(adapter, "measurement_key", None)
    measurement_key = (
        str(key_factory(candidate, value, value_index))
        if callable(key_factory)
        else value.measurement_key or value.band
    ).strip()
    if not measurement_key:
        raise ValueError(f"{adapter.name} measurement has no stable key")
    resolution = default_resolution(adapter.name, value.band)
    values = {
        "run_id": run_id,
        "target_id": target_id,
        "raw_row_id": raw_row_id,
        "detection_id": detection.id,
        "measurement_key": measurement_key,
        "provider": adapter.name,
        "source_id": candidate.source_id,
        "band": value.band,
        "value": value.value,
        "error": value.error,
        "systematic_error": value.systematic_error,
        "upper_limit": value.upper_limit,
        "unit": value.unit,
        "bibcode": value.bibcode,
        "quality": value.quality,
        "note1": value.note1,
        "note2": value.note2,
        "private": value.private,
        "excluded": value.excluded,
        "exclusion_reason": value.exclusion_reason,
        "resolution_major_arcsec": (
            value.resolution_major_arcsec if value.resolution_major_arcsec is not None
            else None if resolution is None else resolution.major_arcsec
        ),
        "resolution_minor_arcsec": (
            value.resolution_minor_arcsec if value.resolution_minor_arcsec is not None
            else None if resolution is None else resolution.minor_arcsec
        ),
        "resolution_kind": value.resolution_kind or (
            None if resolution is None else resolution.kind
        ),
        "resolution_reference": value.resolution_reference or (
            None if resolution is None else resolution.reference
        ),
        "ownership_scope": ownership_scope,
        "blend_state": blend_state,
        "blend_reason": value.blend_reason,
    }
    provider_values = {
        key: values[key]
        for key in (
            "provider", "source_id", "band", "value", "error",
            "systematic_error", "upper_limit", "unit", "bibcode",
            "quality", "note1", "note2", "private",
            "resolution_major_arcsec", "resolution_minor_arcsec",
            "resolution_kind", "resolution_reference",
            "ownership_scope", "blend_state", "blend_reason",
        )
    }
    session.execute(
        sqlite_insert(NormalizedMeasurement)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["detection_id", "measurement_key"],
            set_=provider_values,
        )
    )
    measurement = session.scalar(select(NormalizedMeasurement).where(
        NormalizedMeasurement.detection_id == detection.id,
        NormalizedMeasurement.measurement_key == measurement_key,
    ))
    if measurement is None:
        raise RuntimeError("failed to create or retrieve canonical measurement")
    return measurement


def _summary(
    items: list[DetectionNormalizationItem],
) -> DetectionNormalizationSummary:
    return DetectionNormalizationSummary(
        detection_count=len(items),
        completed=sum(item.status == "completed" for item in items),
        no_measurements=sum(item.status == "no_measurements" for item in items),
        failed=sum(item.status == "failed" for item in items),
        measurement_count=sum(item.measurement_count for item in items),
        items=tuple(items),
    )
