"""Canonical persistence of one detection encounter and its measurements."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .catalog_ingestion import canonical_detection
from .catalog_normalization import normalize_detection
from .catalog_types import (
    CatalogAdapter,
    CatalogCandidate,
    DetectionNormalizationItem,
)
from .models import CatalogDetection, RawCatalogRow


@dataclass(frozen=True)
class DetectionIngestResult:
    detection: CatalogDetection
    raw_row: RawCatalogRow
    normalization: DetectionNormalizationItem


class DetectionIngestor:
    """Store a canonical detection, query encounter, and normalized values."""

    @staticmethod
    def ingest(
        session: Session,
        *,
        adapter: CatalogAdapter,
        candidate: CatalogCandidate,
        run_id: int,
        target_id: int,
        accepted: bool,
        separation_arcsec: float,
        score: float,
        strict: bool,
        raw_source_id: str | None = None,
    ) -> DetectionIngestResult:
        detection = canonical_detection(session, adapter, candidate)
        raw_row = RawCatalogRow(
            run_id=run_id,
            detection_id=detection.id,
            source_id=raw_source_id or candidate.source_id,
            ra_deg=candidate.ra_deg,
            dec_deg=candidate.dec_deg,
            epoch=candidate.epoch,
            separation_arcsec=separation_arcsec,
            score=score,
            accepted=accepted,
            payload_json=json.dumps(
                candidate.payload,
                sort_keys=True,
                ensure_ascii=False,
            ),
        )
        session.add(raw_row)
        session.flush()
        normalization = normalize_detection(
            session,
            adapter=adapter,
            candidate=candidate,
            detection=detection,
            run_id=run_id,
            target_id=target_id,
            raw_row_id=raw_row.id,
            strict=strict,
        )
        return DetectionIngestResult(detection, raw_row, normalization)
