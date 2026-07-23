from __future__ import annotations

import hashlib
import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from .dirty import pending_export_targets
from .database import make_session_factory
from .export import export_ipac
from .models import Sample, SampleExportItem, SampleExportRun
from .progress import NULL_PROGRESS, ProgressReporter
from .samples import SampleService


@dataclass(frozen=True)
class SampleExportSummary:
    run_id: int
    sample: str
    target_count: int
    exported: int
    skipped: int
    failed: int
    manifest: str
    joint_fit_manifest: str


class SampleExportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        reporter: ProgressReporter | None = None,
        workers: int = 1,
    ):
        self.sessions = session_factory
        self.reporter = reporter or NULL_PROGRESS
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.workers = workers

    def export(self, sample_name: str, output_dir: str | Path) -> SampleExportSummary:
        output_dir = Path(output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        members = SampleService(self.sessions).members(sample_name)
        with self.sessions.begin() as session:
            sample = session.scalar(select(Sample).where(Sample.name == sample_name))
            if sample is None:
                raise KeyError(f"sample not found: {sample_name}")
            revision = session.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one()
            run = SampleExportRun(
                sample_id=sample.id,
                output_dir=str(output_dir),
                database_revision=revision,
                status="running",
            )
            session.add(run)
            session.flush()
            run_id = run.id
            started_at = run.started_at

        dirty_ids = {
            target.id for target, _count, _since in pending_export_targets(
                self.sessions, sample=sample_name,
            )
        }
        items = []
        tasks = []
        for target in members:
            output = output_dir / f"{target.sdbid}-rawphot.txt"
            previous = (
                None if target.id in dirty_ids
                else self._previous_success(sample.id, target.id, output)
            )
            status, digest, error = "exported", None, None
            if target.id not in dirty_ids and previous is not None:
                digest = self._sha256(output)
                if digest == previous.sha256:
                    status = "skipped"
            if status == "exported":
                tasks.append((target.id, target.sdbid, output))
                continue
            items.append({
                "target_id": target.id,
                "sdbid": target.sdbid,
                "status": status,
                "output": str(output),
                "sha256": digest,
                "error": error,
            })

        database = str(self.sessions.kw["bind"].url.database)
        worker_tasks = [
            (database, target_id, sdbid, str(output))
            for target_id, sdbid, output in tasks
        ]
        if self.workers == 1 or len(worker_tasks) < 2:
            exported_items = map(_export_target_task, worker_tasks)
        else:
            executor = ProcessPoolExecutor(max_workers=min(self.workers, len(worker_tasks)))
            exported_items = executor.map(_export_target_task, worker_tasks)
        try:
            items.extend(self.reporter.iter(
                exported_items,
                desc=f"Exporting sample {sample_name}",
                total=len(worker_tasks),
                unit="target",
            ))
        finally:
            if self.workers > 1 and len(worker_tasks) >= 2:
                executor.shutdown()

        items.sort(key=lambda item: item["sdbid"])
        for item in items:
            with self.sessions.begin() as session:
                session.add(SampleExportItem(
                    run_id=run_id,
                    target_id=item["target_id"],
                    status=item["status"],
                    output_path=item["output"],
                    sha256=item["sha256"],
                    error=item["error"],
                ))

        failed = sum(item["status"] == "failed" for item in items)
        manifest_path = output_dir / f"sample-{run_id}-manifest.json"
        joint_fit_path = output_dir / f"sample-{run_id}-joint-fit.json"
        completed_at = datetime.now(timezone.utc)
        from .joint_fit_manifest import write_joint_fit_manifest

        write_joint_fit_manifest(
            self.sessions,
            joint_fit_path,
            sample=sample_name,
            legacy_exports=items,
            generated_at=completed_at,
            database_revision=revision,
        )
        joint_fit_sha256 = self._sha256(joint_fit_path)
        manifest = {
            "run_id": run_id,
            "sample": sample_name,
            "database_revision": revision,
            "status": "partial" if failed else "completed",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "target_count": len(items),
            "exported": sum(item["status"] == "exported" for item in items),
            "skipped": sum(item["status"] == "skipped" for item in items),
            "failed": failed,
            "joint_fit": {
                "schema": "sdb-joint-fit-manifest",
                "schema_version": 1,
                "path": str(joint_fit_path),
                "sha256": joint_fit_sha256,
            },
            "items": items,
        }
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(manifest_path)
        with self.sessions.begin() as session:
            stored = session.get(SampleExportRun, run_id)
            stored.status = manifest["status"]
            stored.manifest_path = str(manifest_path)
            stored.completed_at = completed_at
        return SampleExportSummary(
            run_id=run_id,
            sample=sample_name,
            target_count=len(items),
            exported=manifest["exported"],
            skipped=manifest["skipped"],
            failed=failed,
            manifest=str(manifest_path),
            joint_fit_manifest=str(joint_fit_path),
        )

    def _previous_success(self, sample_id: int, target_id: int, output: Path):
        if not output.is_file():
            return None
        with self.sessions() as session:
            return session.scalar(
                select(SampleExportItem)
                .join(SampleExportRun, SampleExportRun.id == SampleExportItem.run_id)
                .where(
                    SampleExportRun.sample_id == sample_id,
                    SampleExportItem.target_id == target_id,
                    SampleExportItem.output_path == str(output),
                    SampleExportItem.status.in_(("exported", "skipped")),
                )
                .order_by(SampleExportItem.id.desc())
                .limit(1)
            )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _export_target_task(task: tuple[str, int, str, str]) -> dict[str, object]:
    database, target_id, sdbid, output_value = task
    output = Path(output_value)
    try:
        sessions = make_session_factory(database)
        export_ipac(sessions, target_id, output)
        digest = SampleExportService._sha256(output)
        status, error = "exported", None
    except Exception as exc:
        digest, status, error = None, "failed", str(exc)
    return {
        "target_id": target_id,
        "sdbid": sdbid,
        "status": status,
        "output": str(output),
        "sha256": digest,
        "error": error,
    }
