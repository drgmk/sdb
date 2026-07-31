from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy.table import Table
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    CatalogRun,
    CatalogDetection,
    CuratedAssociationAction,
    CuratedRecord,
    DatasetRevision,
    ExportDirtyTarget,
    ExternalIdentifier,
    MeasurementEligibilityAction,
    NormalizedMeasurement,
    RawCatalogRow,
    Target,
)
from .decisions import DecisionContext
from .dirty import clear_export_dirty, mark_export_dirty
from .identifiers import normalize_identifier
from .targets import resolve_target
from .vocabulary import ProviderRunStatus


REQUIRED_SUBMM_COLUMNS = {
    "record_no", "id", "wav", "instrument", "fnu_mjy", "err_mjy",
    "sig3lim", "ref", "exclude",
}


@dataclass(frozen=True)
class DatasetImportResult:
    revision_id: int
    dataset: str
    rows: int
    new: int
    changed: int
    removed: int
    matched: int
    unresolved: int
    ambiguous: int
    affected_targets: int
    unchanged_revision: bool = False


@dataclass(frozen=True)
class DatasetReconcileResult:
    revision_id: int
    dataset: str
    newly_matched: int
    unresolved: int
    ambiguous: int
    affected_targets: int


def _value(value):
    if value is None or np.ma.is_masked(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _text(value) -> str | None:
    value = _value(value)
    if value is None:
        return None
    result = str(value).strip()
    return result if result and result.lower() != "null" else None


def _float(value, *, default: float | None = None) -> float | None:
    value = _text(value)
    if value is None:
        return default
    try:
        result = float(value)
    except ValueError as error:
        raise ValueError(f"expected a number, got {value!r}") from error
    if not math.isfinite(result):
        raise ValueError(f"expected a finite number, got {value!r}")
    return result


def _truthy(value) -> bool:
    value = (_text(value) or "").lower()
    return value not in {"", "0", "false", "no", "n"}


def _payload(row) -> dict[str, object]:
    return {str(name): _value(row[name]) for name in row.colnames}


class CuratedDatasetService:
    """Import source-controlled photometry files as atomic dataset revisions."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def import_submm_obs(self, source: str | Path) -> DatasetImportResult:
        source = Path(source).expanduser().resolve()
        content = source.read_bytes()
        source_hash = hashlib.sha256(content).hexdigest()
        table = Table.read(source, format="ascii.ipac")
        missing = REQUIRED_SUBMM_COLUMNS - set(table.colnames)
        if missing:
            raise ValueError(f"submm_obs is missing columns: {', '.join(sorted(missing))}")
        payloads = self._validate_rows(table)

        with self.session_factory() as session, session.begin():
            existing = session.scalar(
                select(DatasetRevision).where(
                    DatasetRevision.dataset == "submm_obs",
                    DatasetRevision.source_sha256 == source_hash,
                )
            )
            if existing is not None:
                return self._result(session, existing, unchanged_revision=True)

            previous = session.scalar(
                select(DatasetRevision).where(
                    DatasetRevision.dataset == "submm_obs",
                    DatasetRevision.is_current.is_(True),
                )
            )
            previous_rows = {}
            if previous is not None:
                previous_rows = {
                    row.record_no: row
                    for row in session.scalars(
                        select(CuratedRecord).where(CuratedRecord.revision_id == previous.id)
                    )
                }

            identifiers: dict[str, set[int]] = defaultdict(set)
            for identifier in session.scalars(select(ExternalIdentifier)):
                identifiers[identifier.normalized_value].add(identifier.target_id)
            manual_actions = {}
            for action in session.scalars(
                select(CuratedAssociationAction)
                .where(CuratedAssociationAction.dataset == "submm_obs")
                .order_by(CuratedAssociationAction.id)
            ):
                manual_actions[action.record_no] = action

            revision = DatasetRevision(
                dataset="submm_obs",
                source_path=str(source),
                source_sha256=source_hash,
                status="importing",
                row_count=len(payloads),
            )
            session.add(revision)
            session.flush()

            current_numbers = {payload["record_no"] for payload in payloads}
            revision.new_count = sum(number not in previous_rows for number in current_numbers)
            revision.changed_count = sum(
                number in previous_rows and payload["row_sha256"] != previous_rows[number].row_sha256
                for number, payload in ((item["record_no"], item) for item in payloads)
            )
            revision.removed_count = len(set(previous_rows) - current_numbers)

            records_by_target: dict[int, list[tuple[CuratedRecord, dict[str, object]]]] = defaultdict(list)
            dirty: dict[int, set[str]] = defaultdict(set)
            matched = unresolved = ambiguous = 0
            for item in payloads:
                manual = manual_actions.get(item["record_no"])
                candidates = identifiers.get(normalize_identifier(str(item["id"])), set())
                if manual is not None and manual.action == "associate":
                    if session.get(Target, manual.target_id) is None:
                        raise ValueError(
                            f"manual association for record {item['record_no']} references a missing target"
                        )
                    target_id = manual.target_id
                    status, method, message = "matched", "manual", manual.reason
                    matched += 1
                elif manual is not None:
                    target_id = None
                    status, method, message = "unresolved", "manual_unassociated", manual.reason
                    unresolved += 1
                elif len(candidates) == 1:
                    target_id = next(iter(candidates))
                    status, method, message = "matched", "exact_alias", None
                    matched += 1
                elif candidates:
                    target_id = None
                    status, method = "ambiguous", None
                    message = f"identifier matches {len(candidates)} targets"
                    ambiguous += 1
                else:
                    target_id = None
                    status, method, message = "unresolved", None, "identifier is not a known target alias"
                    unresolved += 1
                record = CuratedRecord(
                    revision_id=revision.id,
                    record_no=item["record_no"],
                    row_sha256=item["row_sha256"],
                    source_identifier=str(item["id"]),
                    payload_json=item["payload_json"],
                    target_id=target_id,
                    association_status=status,
                    association_method=method,
                    association_message=message,
                )
                session.add(record)
                session.flush()
                if target_id is not None:
                    records_by_target[target_id].append((record, item))

                old = previous_rows.get(item["record_no"])
                if old is None:
                    if target_id is not None:
                        dirty[target_id].add("new record")
                elif old.row_sha256 != item["row_sha256"] or old.target_id != target_id:
                    if old.target_id is not None:
                        dirty[old.target_id].add("changed or reassociated record")
                    if target_id is not None:
                        dirty[target_id].add("changed or reassociated record")

            for number in set(previous_rows) - current_numbers:
                old = previous_rows[number]
                if old.target_id is not None:
                    dirty[old.target_id].add("removed record")

            revision.unresolved_count = unresolved
            revision.ambiguous_count = ambiguous
            session.execute(
                update(DatasetRevision)
                .where(DatasetRevision.dataset == "submm_obs", DatasetRevision.is_current.is_(True))
                .values(is_current=False)
            )
            session.execute(
                update(CatalogRun)
                .where(CatalogRun.provider == "submm_obs", CatalogRun.is_current.is_(True))
                .values(is_current=False)
            )
            self._materialize(session, revision, records_by_target)
            for target_id, reasons in dirty.items():
                mark_export_dirty(
                    session,
                    target_id,
                    source_type="dataset",
                    source_id=revision.id,
                    reason=", ".join(sorted(reasons)),
                )
            revision.status = "active"
            revision.is_current = True
            revision.completed_at = datetime.now(timezone.utc)
            session.flush()
            return self._result(session, revision, matched=matched)

    @staticmethod
    def _validate_rows(table: Table) -> list[dict[str, object]]:
        seen = set()
        result = []
        for position, row in enumerate(table, start=1):
            record_no = int(row["record_no"])
            if record_no < 1 or record_no in seen:
                raise ValueError(f"invalid or duplicate record_no {record_no} at row {position}")
            seen.add(record_no)
            identifier = _text(row["id"])
            wavelength = _float(row["wav"])
            flux = _float(row["fnu_mjy"])
            if not identifier or wavelength is None or flux is None:
                raise ValueError(f"record {record_no} requires id, wav, and fnu_mjy")
            payload = _payload(row)
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            result.append({
                **payload,
                "record_no": record_no,
                "id": identifier,
                "wav": wavelength,
                "fnu_mjy": flux,
                "payload_json": encoded,
                "row_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            })
        return result

    @staticmethod
    def _materialize(session, revision, records_by_target):
        release = f"sha256:{revision.source_sha256[:16]}"
        for target_id, values in records_by_target.items():
            target = session.get(Target, target_id)
            run = CatalogRun(
                target_id=target_id,
                provider="submm_obs",
                release=release,
                status=ProviderRunStatus.MATCH,
                is_current=True,
                query_ra_deg=target.ra2000_deg,
                query_dec_deg=target.dec2000_deg,
                query_epoch=2000.0,
                candidate_count=len(values),
                selected_source_id="curated revision",
                completed_at=datetime.now(timezone.utc),
            )
            session.add(run)
            session.flush()
            for record, item in values:
                source_id = f"submm_obs:{record.record_no}"
                detection = CatalogDetection(
                    provider="submm_obs",
                    release=release,
                    detection_key=str(record.record_no),
                    source_id=str(item["id"]),
                    ra_deg=target.ra2000_deg,
                    dec_deg=target.dec2000_deg,
                    epoch=2000.0,
                    payload_json=record.payload_json,
                    normalization_status="completed",
                    normalized_at=datetime.now(timezone.utc),
                )
                session.add(detection)
                session.flush()
                raw = RawCatalogRow(
                    run_id=run.id,
                    detection_id=detection.id,
                    source_id=source_id,
                    ra_deg=target.ra2000_deg,
                    dec_deg=target.dec2000_deg,
                    epoch=2000.0,
                    separation_arcsec=0.0,
                    score=100.0,
                    accepted=True,
                    payload_json=record.payload_json,
                )
                session.add(raw)
                session.flush()
                session.add(NormalizedMeasurement(
                    run_id=run.id,
                    target_id=target_id,
                    raw_row_id=raw.id,
                    detection_id=detection.id,
                    measurement_key=f"WAV{int(round(float(item['wav'])))}:0",
                    provider="submm_obs",
                    source_id=str(item["id"]),
                    band=f"WAV{int(round(float(item['wav'])))}",
                    value=float(item["fnu_mjy"]),
                    error=float(_float(item.get("err_mjy"), default=0.0) or 0.0),
                    systematic_error=0.0,
                    upper_limit=_truthy(item.get("sig3lim")),
                    unit="mJy",
                    bibcode=_text(item.get("ref")) or "",
                    note1=f"Instr:{_text(item.get('instrument')) or ''}",
                    note2=f"Name:{_text(item.get('name')) or ''}",
                    excluded=_truthy(item.get("exclude")),
                    exclusion_reason="submm_obs exclude flag" if _truthy(item.get("exclude")) else None,
                    ownership_scope="component",
                    blend_state="clear",
                ))

    @staticmethod
    def _result(session, revision, *, matched=None, unchanged_revision=False):
        if matched is None:
            matched = session.scalar(
                select(func.count(CuratedRecord.id)).where(
                    CuratedRecord.revision_id == revision.id,
                    CuratedRecord.association_status == "matched",
                )
            ) or 0
        affected = session.scalar(
            select(func.count(ExportDirtyTarget.id)).where(
                ExportDirtyTarget.source_type == "dataset",
                ExportDirtyTarget.source_id == str(revision.id),
            )
        ) or 0
        return DatasetImportResult(
            revision.id, revision.dataset, revision.row_count,
            revision.new_count, revision.changed_count, revision.removed_count,
            matched, revision.unresolved_count, revision.ambiguous_count,
            affected, unchanged_revision,
        )

    def revisions(self, dataset: str = "submm_obs") -> list[DatasetRevision]:
        with self.session_factory() as session:
            return list(session.scalars(
                select(DatasetRevision)
                .where(DatasetRevision.dataset == dataset)
                .order_by(DatasetRevision.id.desc())
            ))

    def reconcile(self, dataset: str = "submm_obs") -> DatasetReconcileResult:
        with self.session_factory() as session, session.begin():
            revision = session.scalar(select(DatasetRevision).where(
                DatasetRevision.dataset == dataset,
                DatasetRevision.is_current.is_(True),
            ))
            if revision is None:
                raise KeyError(f"no current revision for dataset: {dataset}")

            identifiers: dict[str, set[int]] = defaultdict(set)
            for identifier in session.scalars(select(ExternalIdentifier)):
                identifiers[identifier.normalized_value].add(identifier.target_id)
            records = list(session.scalars(
                select(CuratedRecord).where(CuratedRecord.revision_id == revision.id)
            ))
            newly_matched_targets = set()
            newly_matched_records = 0
            for record in records:
                if record.association_status == "matched" or (
                    record.association_method or ""
                ).startswith("manual"):
                    continue
                candidates = identifiers.get(normalize_identifier(record.source_identifier), set())
                if len(candidates) == 1:
                    record.target_id = next(iter(candidates))
                    record.association_status = "matched"
                    record.association_method = "exact_alias"
                    record.association_message = None
                    newly_matched_targets.add(record.target_id)
                    newly_matched_records += 1
                elif len(candidates) > 1:
                    record.association_status = "ambiguous"
                    record.association_message = f"identifier matches {len(candidates)} targets"
                else:
                    record.association_status = "unresolved"
                    record.association_message = "identifier is not a known target alias"

            revision.unresolved_count = sum(r.association_status == "unresolved" for r in records)
            revision.ambiguous_count = sum(r.association_status == "ambiguous" for r in records)
            records_by_target = defaultdict(list)
            for record in records:
                if record.target_id is None or record.association_status != "matched":
                    continue
                item = json.loads(record.payload_json)
                records_by_target[record.target_id].append((record, item))

            if newly_matched_targets:
                session.execute(
                    update(CatalogRun)
                    .where(CatalogRun.provider == dataset, CatalogRun.is_current.is_(True))
                    .values(is_current=False)
                )
                self._materialize(session, revision, records_by_target)
                for target_id in newly_matched_targets:
                    self._dirty(session, revision.id, target_id, "newly associated record")
            session.flush()
            return DatasetReconcileResult(
                revision.id,
                dataset,
                newly_matched_records,
                revision.unresolved_count,
                revision.ambiguous_count,
                len(newly_matched_targets),
            )

    def unresolved(self, dataset: str = "submm_obs") -> list[CuratedRecord]:
        with self.session_factory() as session:
            return list(session.scalars(
                select(CuratedRecord)
                .join(DatasetRevision, DatasetRevision.id == CuratedRecord.revision_id)
                .where(
                    DatasetRevision.dataset == dataset,
                    DatasetRevision.is_current.is_(True),
                    CuratedRecord.association_status != "matched",
                )
                .order_by(CuratedRecord.record_no)
            ))

    def associate(
        self,
        dataset: str,
        record_no: int,
        target_reference: str | int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> CuratedAssociationAction:
        with self.session_factory() as session, session.begin():
            revision, record = self._current_record(session, dataset, record_no)
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Associated {dataset} record {record_no} with {target.sdbid}"
                ),
            )
            old_target_id = record.target_id
            action = CuratedAssociationAction(
                dataset=dataset,
                record_no=record_no,
                action="associate",
                target_id=target.id,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(action)
            record.target_id = target.id
            record.association_status = "matched"
            record.association_method = "manual"
            record.association_message = decision.reason
            self._refresh_counts(session, revision)
            self._rematerialize(session, revision)
            if old_target_id is not None and old_target_id != target.id:
                self._dirty(session, revision.id, old_target_id, "manually reassociated record")
            self._dirty(session, revision.id, target.id, "manually associated record")
            session.flush()
            return action

    def unassociate(
        self,
        dataset: str,
        record_no: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> CuratedAssociationAction:
        with self.session_factory() as session, session.begin():
            revision, record = self._current_record(session, dataset, record_no)
            old_target_id = record.target_id
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=f"Removed association for {dataset} record {record_no}",
            )
            action = CuratedAssociationAction(
                dataset=dataset,
                record_no=record_no,
                action="unassociate",
                target_id=old_target_id,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(action)
            record.target_id = None
            record.association_status = "unresolved"
            record.association_method = "manual_unassociated"
            record.association_message = decision.reason
            self._refresh_counts(session, revision)
            self._rematerialize(session, revision)
            if old_target_id is not None:
                self._dirty(session, revision.id, old_target_id, "manually unassociated record")
            session.flush()
            return action

    def set_record_override(
        self,
        dataset: str,
        record_no: int,
        *,
        excluded: bool,
        actor: str | None,
        reason: str | None = None,
    ) -> MeasurementEligibilityAction:
        with self.session_factory() as session, session.begin():
            revision, record = self._current_record(session, dataset, record_no)
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"{'Excluded' if excluded else 'Included'} {dataset} "
                    f"record {record_no} photometry"
                ),
            )
            measurement = session.scalar(
                select(NormalizedMeasurement)
                .join(
                    RawCatalogRow,
                    RawCatalogRow.id
                    == NormalizedMeasurement.raw_row_id,
                )
                .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
                .where(
                    CatalogRun.is_current.is_(True),
                    CatalogRun.provider == dataset,
                    RawCatalogRow.source_id == f"{dataset}:{record_no}",
                )
            )
            if measurement is None:
                raise KeyError(
                    f"current measurement not found: {dataset} record {record_no}"
                )
            action = MeasurementEligibilityAction(
                measurement_id=measurement.id,
                excluded=excluded,
                actor=decision.actor,
                reason=decision.reason,
            )
            session.add(action)
            if record.target_id is not None:
                self._dirty(
                    session,
                    revision.id,
                    record.target_id,
                    "record measurement eligibility changed",
                )
            session.flush()
            return action

    def pending(self, dataset: str = "submm_obs") -> list[tuple[ExportDirtyTarget, Target]]:
        with self.session_factory() as session:
            revision_ids = [
                str(revision_id)
                for revision_id in session.scalars(
                    select(DatasetRevision.id).where(DatasetRevision.dataset == dataset)
                )
            ]
            if not revision_ids:
                return []
            return list(session.execute(
                select(ExportDirtyTarget, Target)
                .join(Target, Target.id == ExportDirtyTarget.target_id)
                .where(
                    ExportDirtyTarget.source_type == "dataset",
                    ExportDirtyTarget.source_id.in_(revision_ids),
                    ExportDirtyTarget.exported_at.is_(None),
                )
                .order_by(ExportDirtyTarget.id)
            ))

    def mark_exported(self, dataset: str, target_reference: str | int) -> int:
        with self.session_factory() as session, session.begin():
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            return clear_export_dirty(session, target.id)

    @staticmethod
    def _current_record(session, dataset, record_no):
        revision = session.scalar(select(DatasetRevision).where(
            DatasetRevision.dataset == dataset,
            DatasetRevision.is_current.is_(True),
        ))
        if revision is None:
            raise KeyError(f"no current revision for dataset: {dataset}")
        record = session.scalar(select(CuratedRecord).where(
            CuratedRecord.revision_id == revision.id,
            CuratedRecord.record_no == record_no,
        ))
        if record is None:
            raise KeyError(f"record not found: {dataset}:{record_no}")
        return revision, record

    @staticmethod
    def _refresh_counts(session, revision):
        revision.unresolved_count = session.scalar(select(func.count(CuratedRecord.id)).where(
            CuratedRecord.revision_id == revision.id,
            CuratedRecord.association_status == "unresolved",
        )) or 0
        revision.ambiguous_count = session.scalar(select(func.count(CuratedRecord.id)).where(
            CuratedRecord.revision_id == revision.id,
            CuratedRecord.association_status == "ambiguous",
        )) or 0

    def _rematerialize(self, session, revision):
        records_by_target = defaultdict(list)
        records = session.scalars(select(CuratedRecord).where(
            CuratedRecord.revision_id == revision.id,
            CuratedRecord.association_status == "matched",
            CuratedRecord.target_id.is_not(None),
        ))
        for record in records:
            records_by_target[record.target_id].append((record, json.loads(record.payload_json)))
        session.execute(
            update(CatalogRun)
            .where(CatalogRun.provider == revision.dataset, CatalogRun.is_current.is_(True))
            .values(is_current=False)
        )
        self._materialize(session, revision, records_by_target)

    @staticmethod
    def _dirty(session, revision_id, target_id, reason):
        mark_export_dirty(
            session,
            target_id,
            source_type="dataset",
            source_id=revision_id,
            reason=reason,
        )
