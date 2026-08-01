from __future__ import annotations

import csv
import hashlib
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .catalogs.acquisition import CatalogAcquisitionService
from .ingestion import TargetIngestionPlan
from .metadata import MetadataService
from .models.batch import ImportItem, ImportJob, ImportRun
from .progress import NULL_PROGRESS, ProgressReporter
from .providers import ProviderError
from .service import AddRequest, IdentityService, UnresolvedTarget
from .vocabulary import PROVIDER_REVIEW_STATUSES, ProviderRunStatus


STAGE_ORDER = ("identity", "simbad", "gaia_dr3", "tycho2", "2mass", "allwise")
SUCCESS_STATUSES = {"succeeded", ProviderRunStatus.NO_MATCH}
FAILURE_STATUSES = set(PROVIDER_REVIEW_STATUSES)
TERMINAL_STATUSES = SUCCESS_STATUSES | FAILURE_STATUSES | {"skipped"}
_PROVIDER_JOB_STATUS = {
    ProviderRunStatus.MATCH: "succeeded",
    ProviderRunStatus.NO_MATCH: ProviderRunStatus.NO_MATCH.value,
    ProviderRunStatus.AMBIGUOUS: ProviderRunStatus.AMBIGUOUS.value,
    ProviderRunStatus.TRANSIENT_FAILURE:
        ProviderRunStatus.TRANSIENT_FAILURE.value,
    ProviderRunStatus.PERMANENT_FAILURE:
        ProviderRunStatus.PERMANENT_FAILURE.value,
}


@dataclass(frozen=True)
class BatchSummary:
    run_id: int
    status: str
    item_count: int
    job_counts: dict[str, int]


class BatchService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        identity_factory: Callable[[], IdentityService],
        metadata_factory: Callable[[], MetadataService],
        catalog_factory: Callable[[], CatalogAcquisitionService],
        workers: dict[str, int] | None = None,
        reporter: ProgressReporter | None = None,
    ):
        self.sessions = session_factory
        self.identity_factory = identity_factory
        self.metadata_factory = metadata_factory
        self.catalog_factory = catalog_factory
        self.workers = {
            "identity": 2, "simbad": 2, "gaia_dr3": 4,
            "tycho2": 4, "2mass": 4, "allwise": 4,
        }
        if workers:
            for stage, count in workers.items():
                if stage not in STAGE_ORDER or count < 1:
                    raise ValueError(f"invalid worker setting {stage}={count}")
                self.workers[stage] = count
        self.reporter = reporter or NULL_PROGRESS

    def create(self, path: str | Path, *, refresh: Iterable[str] = ()) -> BatchSummary:
        path = Path(path).expanduser().resolve()
        content = path.read_bytes()
        text = content.decode("utf-8-sig")
        delimiter = self._delimiter(path, text)
        requested = tuple(dict.fromkeys(refresh))
        unknown = set(requested) - {
            "simbad", "gaia_dr3", "tycho2", "2mass", "allwise",
        }
        if unknown:
            raise ValueError(f"unknown refresh stages: {', '.join(sorted(unknown))}")
        stages = ("identity", *[stage for stage in STAGE_ORDER[1:] if stage in requested])
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("input file has no header")
        rows = list(reader)
        with self.sessions.begin() as session:
            run = ImportRun(
                source_path=str(path),
                source_sha256=hashlib.sha256(content).hexdigest(),
                delimiter="tab" if delimiter == "\t" else delimiter,
                requested_stages_json=json.dumps(stages),
                workers_json=json.dumps(self.workers, sort_keys=True),
                status="pending",
                item_count=len(rows),
            )
            session.add(run)
            session.flush()
            for row_number, row in enumerate(rows, start=2):
                clean = {str(key): "" if value is None else str(value) for key, value in row.items()}
                raw = json.dumps(clean, sort_keys=True, ensure_ascii=False)
                item = ImportItem(
                    run_id=run.id,
                    row_number=row_number,
                    input_json=raw,
                    dedup_key=hashlib.sha256(raw.encode()).hexdigest(),
                    status="pending",
                )
                session.add(item)
                session.flush()
                for stage in stages:
                    session.add(
                        ImportJob(
                            run_id=run.id,
                            item_id=item.id,
                            stage=stage,
                            status="pending",
                        )
                    )
        return self.status(run.id)

    def execute(self, run_id: int) -> BatchSummary:
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            run = session.get(ImportRun, run_id)
            if run is None:
                raise KeyError(f"import run not found: {run_id}")
            session.execute(
                update(ImportJob)
                .where(ImportJob.run_id == run_id, ImportJob.status == "running")
                .values(status="pending", last_error="interrupted before completion")
            )
            run.status = "running"
            run.started_at = run.started_at or now
            run.completed_at = None
            stages = tuple(json.loads(run.requested_stages_json))

        for stage in STAGE_ORDER:
            if stage not in stages:
                continue
            job_ids = self._eligible_job_ids(run_id, stage)
            if not job_ids:
                continue
            self.reporter.step(f"Import run {run_id}: {stage} stage, {len(job_ids)} jobs")
            if stage == "identity":
                try:
                    identity = self.identity_factory()
                except Exception as error:
                    transient = self._is_transient_exception(error)
                    for job_id in job_ids:
                        self._finish(
                            job_id,
                            (
                                ProviderRunStatus.TRANSIENT_FAILURE
                                if transient
                                else ProviderRunStatus.PERMANENT_FAILURE
                            ),
                            error=f"{type(error).__name__}: {error}",
                            skip_downstream=not transient,
                        )
                    continue
                if hasattr(identity.simbad, "resolve_many"):
                    self._execute_identity_jobs_many(job_ids, identity)
                    continue
                with ThreadPoolExecutor(max_workers=self.workers[stage]) as executor:
                    list(self.reporter.iter(
                        executor.map(self._execute_job, job_ids),
                        desc=f"Import {stage}",
                        total=len(job_ids),
                        unit="job",
                    ))
                continue
            if self._bulk_catalog_stage(stage):
                self._execute_catalog_jobs_many(job_ids, stage)
                continue
            with ThreadPoolExecutor(max_workers=self.workers[stage]) as executor:
                list(self.reporter.iter(
                    executor.map(self._execute_job, job_ids),
                    desc=f"Import {stage}",
                    total=len(job_ids),
                    unit="job",
                ))
        self._finalize(run_id)
        return self.status(run_id)

    def retry(self, run_id: int, *, failures: str = "transient") -> int:
        if failures not in {"transient", "all"}:
            raise ValueError("failures must be 'transient' or 'all'")
        statuses = {ProviderRunStatus.TRANSIENT_FAILURE}
        if failures == "all":
            statuses |= {
                ProviderRunStatus.PERMANENT_FAILURE,
                ProviderRunStatus.AMBIGUOUS,
            }
        with self.sessions.begin() as session:
            run = session.get(ImportRun, run_id)
            if run is None:
                raise KeyError(f"import run not found: {run_id}")
            jobs = list(
                session.scalars(
                    select(ImportJob).where(
                        ImportJob.run_id == run_id,
                        ImportJob.status.in_(statuses),
                    )
                )
            )
            item_ids = {job.item_id for job in jobs if job.stage == "identity"}
            for job in jobs:
                job.status = "pending"
                job.last_error = None
                job.completed_at = None
                job.next_retry_at = None
            if item_ids:
                session.execute(
                    update(ImportJob)
                    .where(
                        ImportJob.item_id.in_(item_ids),
                        ImportJob.status == "skipped",
                    )
                    .values(status="pending", last_error=None, completed_at=None)
                )
            run.status = "pending"
            run.completed_at = None
            return len(jobs)

    def status(self, run_id: int) -> BatchSummary:
        with self.sessions() as session:
            run = session.get(ImportRun, run_id)
            if run is None:
                raise KeyError(f"import run not found: {run_id}")
            counts = Counter(
                session.scalars(
                    select(ImportJob.status).where(ImportJob.run_id == run_id)
                )
            )
            return BatchSummary(run.id, run.status, run.item_count, dict(sorted(counts.items())))

    def _eligible_job_ids(self, run_id: int, stage: str) -> list[int]:
        with self.sessions() as session:
            jobs = list(
                session.scalars(
                    select(ImportJob)
                    .where(
                        ImportJob.run_id == run_id,
                        ImportJob.stage == stage,
                        ImportJob.status == "pending",
                    )
                    .order_by(ImportJob.id)
                )
            )
            if stage == "identity":
                return [job.id for job in jobs]
            result = []
            for job in jobs:
                identity = session.scalar(
                    select(ImportJob).where(
                        ImportJob.item_id == job.item_id,
                        ImportJob.stage == "identity",
                    )
                )
                if identity is not None and identity.status == "succeeded":
                    result.append(job.id)
            return result

    def _execute_identity_jobs_many(
        self,
        job_ids: list[int],
        identity: IdentityService,
    ) -> None:
        requests_by_job: dict[int, AddRequest] = {}
        names = []
        with self.sessions() as session:
            for job_id in job_ids:
                job = session.get(ImportJob, job_id)
                if job is None or job.status != "pending":
                    continue
                item = session.get(ImportItem, job.item_id)
                request = self._request(json.loads(item.input_json))
                requests_by_job[job.id] = request
                if request.name:
                    names.append(request.name)
        try:
            resolved = identity.simbad.resolve_many(tuple(names))
        except ProviderError:
            with ThreadPoolExecutor(max_workers=self.workers["identity"]) as executor:
                list(executor.map(self._execute_job, job_ids))
            return
        worker_state = threading.local()
        def execute_prefetched(job_id: int) -> None:
            request = requests_by_job.get(job_id)
            if request is None:
                return
            # Astroquery clients are stateful; each worker keeps its own
            # identity resolver while consuming immutable prefetched evidence.
            live_identity = getattr(worker_state, "identity", None)
            if live_identity is None:
                live_identity = self.identity_factory()
                worker_state.identity = live_identity
            self._execute_identity_job(
                job_id,
                request,
                live_identity,
                name_resolution=(
                    resolved.get(request.name) if request.name else None
                ),
            )
        with ThreadPoolExecutor(max_workers=self.workers["identity"]) as executor:
            list(self.reporter.iter(
                executor.map(execute_prefetched, job_ids),
                desc="Import identity",
                total=len(job_ids),
                unit="job",
            ))

    def _execute_identity_job(
        self,
        job_id: int,
        request: AddRequest,
        identity: IdentityService,
        *,
        name_resolution: object = None,
    ) -> None:
        with self.sessions.begin() as session:
            job = session.get(ImportJob, job_id)
            if job is None or job.status != "pending":
                return
            job.status = "running"
            job.attempts += 1
            job.started_at = datetime.now(timezone.utc)
            job.completed_at = None
        try:
            result = TargetIngestionPlan(identity=identity).identify(
                request,
                name_resolution=name_resolution,
                prefetched=True,
            )
            self._finish(job_id, "succeeded", target_id=result.target_id)
        except UnresolvedTarget as error:
            status = (
                ProviderRunStatus.TRANSIENT_FAILURE
                if error.transient
                else ProviderRunStatus.PERMANENT_FAILURE
            )
            self._finish(
                job_id,
                status,
                error=str(error),
                skip_downstream=not error.transient,
            )
        except (ValueError, KeyError) as error:
            self._finish(
                job_id,
                ProviderRunStatus.PERMANENT_FAILURE,
                error=str(error),
                skip_downstream=True,
            )
        except Exception as error:
            transient = self._is_transient_exception(error)
            self._finish(
                job_id,
                (
                    ProviderRunStatus.TRANSIENT_FAILURE
                    if transient
                    else ProviderRunStatus.PERMANENT_FAILURE
                ),
                error=f"{type(error).__name__}: {error}",
                skip_downstream=not transient,
            )

    def _bulk_catalog_stage(self, stage: str) -> bool:
        if stage not in {"gaia_dr3", "tycho2", "2mass", "allwise"}:
            return False
        service = self.catalog_factory()
        return hasattr(service.adapters.get(stage), "query_many")

    def _execute_catalog_jobs_many(self, job_ids: list[int], stage: str) -> None:
        target_jobs: dict[int, list[int]] = {}
        skipped_jobs = []
        with self.sessions.begin() as session:
            for job_id in job_ids:
                job = session.get(ImportJob, job_id)
                if job is None or job.status != "pending":
                    continue
                item = session.get(ImportItem, job.item_id)
                if item.target_id is None:
                    skipped_jobs.append(job.id)
                    continue
                job.status = "running"
                job.attempts += 1
                job.started_at = datetime.now(timezone.utc)
                job.completed_at = None
                target_jobs.setdefault(item.target_id, []).append(job.id)

        for job_id in self.reporter.iter(
            skipped_jobs,
            desc=f"Import {stage} skipped",
            total=len(skipped_jobs),
            unit="job",
        ):
            self._finish(
                job_id,
                "skipped",
                error="identity stage did not produce a target",
            )
        if not target_jobs:
            return

        try:
            self.reporter.step(f"Import {stage}: bulk refresh for {len(target_jobs)} targets")
            results = self.catalog_factory().refresh_many(target_jobs, stage)
        except Exception as error:
            transient = self._is_transient_exception(error)
            for grouped_job_ids in target_jobs.values():
                for job_id in grouped_job_ids:
                    self._finish(
                        job_id,
                        (
                            ProviderRunStatus.TRANSIENT_FAILURE
                            if transient
                            else ProviderRunStatus.PERMANENT_FAILURE
                        ),
                        error=f"{type(error).__name__}: {error}",
                    )
            return

        result_by_target = {result.target_id: result for result in results}
        for target_id, grouped_job_ids in self.reporter.iter(
            target_jobs.items(),
            desc=f"Import {stage} results",
            total=len(target_jobs),
            unit="target",
        ):
            result = result_by_target.get(target_id)
            if result is None:
                status = ProviderRunStatus.TRANSIENT_FAILURE
                error = f"{stage} bulk refresh returned no result for target {target_id}"
            else:
                status = _PROVIDER_JOB_STATUS[result.status]
                error = result.error
            for job_id in grouped_job_ids:
                self._finish(job_id, status, error=error)

    def _execute_job(self, job_id: int) -> None:
        with self.sessions.begin() as session:
            job = session.get(ImportJob, job_id)
            if job is None or job.status != "pending":
                return
            job.status = "running"
            job.attempts += 1
            job.started_at = datetime.now(timezone.utc)
            job.completed_at = None
            item = session.get(ImportItem, job.item_id)
            stage = job.stage
            input_data = json.loads(item.input_json)
            target_id = item.target_id

        try:
            if stage == "identity":
                request = self._request(input_data)
                result = TargetIngestionPlan(
                    identity=self.identity_factory(),
                ).identify(request)
                self._finish(job_id, "succeeded", target_id=result.target_id)
                return
            if target_id is None:
                self._finish(job_id, "skipped", error="identity stage did not produce a target")
                return
            if stage == "simbad":
                result = self.metadata_factory().refresh(target_id)
            elif stage in {"gaia_dr3", "tycho2", "2mass", "allwise"}:
                result = self.catalog_factory().refresh(target_id, stage)
            else:
                raise ValueError(f"unknown import stage: {stage}")
            status = _PROVIDER_JOB_STATUS[result.status]
            self._finish(job_id, status, error=getattr(result, "error", None))
        except UnresolvedTarget as error:
            status = (
                ProviderRunStatus.TRANSIENT_FAILURE
                if error.transient
                else ProviderRunStatus.PERMANENT_FAILURE
            )
            self._finish(job_id, status, error=str(error), skip_downstream=not error.transient)
        except (ValueError, KeyError) as error:
            self._finish(
                job_id,
                ProviderRunStatus.PERMANENT_FAILURE,
                error=str(error),
                skip_downstream=stage == "identity",
            )
        except Exception as error:
            transient = self._is_transient_exception(error)
            self._finish(
                job_id,
                (
                    ProviderRunStatus.TRANSIENT_FAILURE
                    if transient
                    else ProviderRunStatus.PERMANENT_FAILURE
                ),
                error=f"{type(error).__name__}: {error}",
                skip_downstream=stage == "identity" and not transient,
            )

    def _finish(
        self,
        job_id: int,
        status: str,
        *,
        error: str | None = None,
        target_id: int | None = None,
        skip_downstream: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            job = session.get(ImportJob, job_id)
            item = session.get(ImportItem, job.item_id)
            job.status = status
            job.last_error = error
            job.completed_at = now
            if status == ProviderRunStatus.TRANSIENT_FAILURE:
                job.next_retry_at = now + timedelta(seconds=min(300, 2 ** job.attempts))
            else:
                job.next_retry_at = None
            if target_id is not None:
                item.target_id = target_id
                item.status = "identified"
                item.error = None
            elif status in FAILURE_STATUSES:
                item.error = error
            if skip_downstream:
                session.execute(
                    update(ImportJob)
                    .where(
                        ImportJob.item_id == item.id,
                        ImportJob.id != job.id,
                        ImportJob.status == "pending",
                    )
                    .values(status="skipped", last_error="identity stage failed", completed_at=now)
                )

    def _finalize(self, run_id: int) -> None:
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            run = session.get(ImportRun, run_id)
            items = list(session.scalars(select(ImportItem).where(ImportItem.run_id == run_id)))
            all_statuses = []
            for item in items:
                statuses = list(
                    session.scalars(
                        select(ImportJob.status).where(ImportJob.item_id == item.id)
                    )
                )
                all_statuses.extend(statuses)
                if any(status in FAILURE_STATUSES for status in statuses):
                    item.status = (
                        "failed"
                        if ProviderRunStatus.AMBIGUOUS not in statuses
                        else "review"
                    )
                elif any(status in {"pending", "running"} for status in statuses):
                    item.status = "pending"
                elif any(status == "skipped" for status in statuses):
                    item.status = "failed"
                else:
                    item.status = "completed"
                    item.completed_at = now
            if all(status in SUCCESS_STATUSES for status in all_statuses):
                run.status = "completed"
            elif any(status in FAILURE_STATUSES or status == "skipped" for status in all_statuses):
                run.status = "partial"
            else:
                run.status = "pending"
            run.completed_at = now

    @staticmethod
    def _request(data: dict[str, str]) -> AddRequest:
        name = data.get("name", "").strip() or None
        ra_text = (data.get("ra", "") or data.get("ra_deg", "")).strip()
        dec_text = (data.get("dec", "") or data.get("dec_deg", "")).strip()
        epoch_text = data.get("epoch", "").strip()
        ra = float(ra_text) if ra_text else None
        dec = float(dec_text) if dec_text else None
        epoch = float(epoch_text) if epoch_text else 2000.0
        return AddRequest(name=name, ra_deg=ra, dec_deg=dec, epoch=epoch, command="batch import")

    @staticmethod
    def _delimiter(path: Path, text: str) -> str:
        if path.suffix.lower() == ".tsv":
            return "\t"
        if path.suffix.lower() == ".csv":
            return ","
        try:
            return csv.Sniffer().sniff(text[:4096], delimiters=",\t").delimiter
        except csv.Error:
            return ","

    @staticmethod
    def _is_transient_exception(error: Exception) -> bool:
        if isinstance(error, (TimeoutError, ConnectionError, OSError)):
            return True
        module = type(error).__module__.split(".", 1)[0]
        return module in {"requests", "urllib3", "pyvo", "httpx", "httpcore"}
