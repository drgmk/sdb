from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import Integer, cast, select
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import propagate_to_epoch
from .catalogs import CatalogQueryContext, CatalogService
from .dirty import mark_export_dirty
from .models import AstrometricSolution, CatalogRun, ExportDirtyTarget, ExternalIdentifier, ReferenceApplicationItem, ReferenceApplicationRecord, ReferenceApplicationRun, Target
from .providers import Astrometry
from .adapters.reference import snapshot_adapter
from .reference_definitions import SNAPSHOT_CATALOGS
from .reference_store import ReferenceStore, utcnow
from .vocabulary import ProviderRunStatus

@dataclass(frozen=True)
class ReferenceApplicationResult:
    application_run_id: int
    provider: str
    snapshot_sha256: str
    targets: int
    refreshed: int
    matched: int
    ambiguous: int
    no_match: int
    catalog_rows: int
    unmatched_rows: int
    unchanged: bool = False


class ReferenceApplicationService:
    def __init__(self, session_factory: sessionmaker[Session], store: ReferenceStore):
        self.sessions = session_factory
        self.store = store

    def apply(
        self, adapter_name: str, *, force: bool = False
    ) -> ReferenceApplicationResult:
        adapter = snapshot_adapter(adapter_name, self.store)
        definition = SNAPSHOT_CATALOGS[adapter_name]
        snapshot = self.store.current_snapshot(adapter.name)
        contexts = self._contexts(adapter.query_epoch)
        target_ids = {context.target_id for context in contexts}

        with self.sessions() as session:
            previous = session.scalar(select(ReferenceApplicationRun).where(
                ReferenceApplicationRun.provider == adapter.name,
                ReferenceApplicationRun.status == "completed",
            ).order_by(ReferenceApplicationRun.id.desc()))
            current_runs = list(session.scalars(select(CatalogRun).where(
                CatalogRun.provider == adapter.name,
                CatalogRun.is_current.is_(True),
            )))
        already_applied = {run.target_id for run in current_runs}
        new_targets = target_ids - already_applied

        if (
            not force
            and previous is not None
            and previous.snapshot_sha256 == snapshot.content_sha256
            and not new_targets
        ):
            return ReferenceApplicationResult(
                previous.id, adapter.name, snapshot.content_sha256,
                len(contexts), 0, 0, 0, 0, previous.row_count,
                previous.unmatched_row_count, True,
            )

        candidates = adapter.query_many(contexts)
        current_hashes = self.store.row_hashes(
            snapshot.id, definition.tables_for_matching, definition.key_columns
        )

        if force or previous is None:
            affected = set(target_ids)
            changed_sources = set(current_hashes)
        else:
            previous_snapshot = self.store.snapshot_by_hash(
                adapter.name, previous.snapshot_sha256
            )
            old_hashes = (
                self.store.row_hashes(
                    previous_snapshot.id,
                    definition.tables_for_matching,
                    definition.key_columns,
                )
                if previous_snapshot is not None else {}
            )
            changed_sources = {
                key for key in set(old_hashes) | set(current_hashes)
                if old_hashes.get(key) != current_hashes.get(key)
            }
            affected = set(new_targets)
            affected.update(
                run.target_id for run in current_runs
                if run.selected_source_id in changed_sources
            )
            for target_id, values in candidates.items():
                if any(value.source_id in changed_sources for value in values):
                    affected.add(target_id)

        if previous is not None and not affected and previous.snapshot_sha256 == snapshot.content_sha256:
            return ReferenceApplicationResult(
                previous.id, adapter.name, snapshot.content_sha256,
                len(contexts), 0, 0, 0, 0, len(current_hashes),
                previous.unmatched_row_count, True,
            )

        with self.sessions() as session, session.begin():
            application = ReferenceApplicationRun(
                provider=adapter.name,
                snapshot_sha256=snapshot.content_sha256,
                status="running",
                target_count=len(contexts),
                row_count=len(current_hashes),
            )
            session.add(application)
            session.flush()
            application_id = application.id

        service = CatalogService(self.sessions, {adapter.name: adapter})
        results = []
        try:
            for context in contexts:
                if context.target_id not in affected:
                    continue
                refreshed = service.refresh(
                    context.target_id,
                    adapter.name,
                    preloaded_candidates=candidates[context.target_id],
                )
                results.append(refreshed)
        except Exception:
            with self.sessions() as session, session.begin():
                application = session.get(ReferenceApplicationRun, application_id)
                application.status = "failed"
                application.completed_at = utcnow()
            raise

        with self.sessions() as session, session.begin():
            application = session.get(ReferenceApplicationRun, application_id)
            for result in results:
                session.add(ReferenceApplicationItem(
                    application_run_id=application_id,
                    target_id=result.target_id,
                    catalog_run_id=result.run_id,
                    status=result.status,
                    selected_source_id=result.selected_source_id,
                    candidate_count=result.candidate_count,
                ))
                mark_export_dirty(
                    session,
                    result.target_id,
                    source_type="reference",
                    source_id=application_id,
                    reason="reference snapshot applied",
                )

            current_selected: dict[str, list[int]] = defaultdict(list)
            for run in session.scalars(select(CatalogRun).where(
                CatalogRun.provider == adapter.name,
                CatalogRun.is_current.is_(True),
                CatalogRun.status == ProviderRunStatus.MATCH,
                CatalogRun.selected_source_id.is_not(None),
            )):
                current_selected[run.selected_source_id].append(run.target_id)
            candidate_targets: dict[str, set[int]] = defaultdict(set)
            for target_id, values in candidates.items():
                for value in values:
                    candidate_targets[value.source_id].add(target_id)
            review_sources = set(candidate_targets) | set(current_selected)
            for source_id in sorted(review_sources):
                row_hash = current_hashes[source_id]
                candidate_ids = sorted(candidate_targets.get(source_id, set()))
                selected_ids = sorted(current_selected.get(source_id, []))
                if selected_ids:
                    status = "matched"
                elif candidate_ids:
                    status = ProviderRunStatus.AMBIGUOUS
                else:
                    status = "unmatched"
                session.add(ReferenceApplicationRecord(
                    application_run_id=application_id,
                    source_identifier=source_id,
                    row_sha256=row_hash,
                    status=status,
                    candidate_target_ids_json=json.dumps(candidate_ids),
                    selected_target_ids_json=json.dumps(selected_ids),
                ))
            application.refreshed_count = len(results)
            application.match_count = sum(
                value.status == ProviderRunStatus.MATCH for value in results
            )
            application.ambiguous_count = sum(
                value.status == ProviderRunStatus.AMBIGUOUS for value in results
            )
            application.no_match_count = sum(
                value.status == ProviderRunStatus.NO_MATCH for value in results
            )
            application.unmatched_row_count = len(current_hashes) - len(candidate_targets)
            application.status = "completed"
            application.completed_at = utcnow()
            result = ReferenceApplicationResult(
                application.id,
                adapter.name,
                snapshot.content_sha256,
                len(contexts),
                application.refreshed_count,
                application.match_count,
                application.ambiguous_count,
                application.no_match_count,
                application.row_count,
                application.unmatched_row_count,
            )
        return result

    def apply_gaspar(self, *, force: bool = False) -> ReferenceApplicationResult:
        return self.apply("gaspar13", force=force)

    def _contexts(self, epoch: float) -> list[CatalogQueryContext]:
        with self.sessions() as session:
            targets = list(session.scalars(select(Target).order_by(Target.id)))
            identifiers: dict[int, list[str]] = defaultdict(list)
            for value in session.scalars(select(ExternalIdentifier).order_by(ExternalIdentifier.id)):
                identifiers[value.target_id].append(value.value)
            contexts = []
            for target in targets:
                solution = session.get(AstrometricSolution, target.canonical_astrometry_id)
                if solution is None:
                    continue
                native = Astrometry(
                    solution.ra_deg,
                    solution.dec_deg,
                    solution.epoch,
                    solution.pm_ra_cosdec_masyr,
                    solution.pm_dec_masyr,
                    solution.parallax_mas,
                    solution.radial_velocity_kms,
                    solution.source,
                    solution.source_id,
                )
                contexts.append(CatalogQueryContext(
                    target.id,
                    target.sdbid,
                    propagate_to_epoch(native, epoch),
                    tuple(identifiers[target.id]),
                ))
            return contexts

    def runs(self, provider: str | None = None) -> list[ReferenceApplicationRun]:
        with self.sessions() as session:
            query = select(ReferenceApplicationRun)
            if provider is not None:
                query = query.where(ReferenceApplicationRun.provider == provider)
            return list(session.scalars(query.order_by(ReferenceApplicationRun.id.desc())))

    def unmatched(
        self,
        application_run_id: int | None = None,
        *,
        provider: str = "gaspar13",
    ) -> list[ReferenceApplicationRecord]:
        with self.sessions() as session:
            if application_run_id is None:
                run = session.scalar(select(ReferenceApplicationRun).where(
                    ReferenceApplicationRun.provider == provider,
                    ReferenceApplicationRun.status == "completed",
                ).order_by(ReferenceApplicationRun.id.desc()))
                if run is None:
                    return []
                application_run_id = run.id
            return list(session.scalars(select(ReferenceApplicationRecord).where(
                ReferenceApplicationRecord.application_run_id == application_run_id,
                ReferenceApplicationRecord.status != "matched",
            ).order_by(ReferenceApplicationRecord.source_identifier)))

    def pending(self, provider: str | None = None):
        with self.sessions() as session:
            query = (
                select(ExportDirtyTarget, Target, ReferenceApplicationRun)
                .join(Target, Target.id == ExportDirtyTarget.target_id)
                .join(
                    ReferenceApplicationRun,
                    ReferenceApplicationRun.id == cast(ExportDirtyTarget.source_id, Integer),
                )
                .where(
                    ExportDirtyTarget.source_type == "reference",
                    ExportDirtyTarget.exported_at.is_(None),
                )
                .order_by(ExportDirtyTarget.id)
            )
            if provider is not None:
                query = query.where(ReferenceApplicationRun.provider == provider)
            return list(session.execute(query))
