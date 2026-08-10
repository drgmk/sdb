from __future__ import annotations

import hashlib
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .database import make_session_factory
from .dirty import export_dirty_watermark, mark_exported_through
from .export import (
    load_target_export_snapshot,
    projection_sha256,
    write_ipac_atomic,
)
from .fitting_groups import fitting_group_report
from .joint_fit import JointFitDefinition, write_joint_fit
from .models.exports import ExportItem, ExportRun
from .progress import NULL_PROGRESS, ProgressReporter
from .selection import resolve_target_selection


@dataclass(frozen=True)
class PackageExportSummary:
    run_id: int
    selection_kind: str
    selection_value: str | None
    selected_target_count: int
    target_count: int
    package_count: int
    exported: int
    skipped: int
    failed: int
    manifest: str


@dataclass(frozen=True)
class _Observation:
    target_id: int
    sdbid: str
    measurement_ids: tuple[int, ...]
    contributor_sdbids: tuple[str, ...]


@dataclass(frozen=True)
class _FitPackage:
    package_id: str
    directory_name: str
    primary_sdbid: str
    selected_sdbids: tuple[str, ...]
    model_sdbids: tuple[str, ...]
    observations: tuple[_Observation, ...]


class PackageExportService:
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

    def export(
        self,
        output_dir: str | Path,
        *,
        target_reference: str | int | None = None,
        sample: str | None = None,
        all_targets: bool = False,
        force: bool = False,
    ) -> PackageExportSummary:
        output_dir = Path(output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        selection = resolve_target_selection(
            self.sessions,
            target_reference=target_reference,
            sample=sample,
            all_targets=all_targets,
        )
        report = fitting_group_report(self.sessions, selection=selection)
        packages = _fit_packages(report)
        with self.sessions.begin() as session:
            revision = session.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one()
            run = ExportRun(
                selection_kind=selection.kind,
                selection_value=selection.value,
                output_dir=str(output_dir),
                database_revision=revision,
                status="running",
            )
            session.add(run)
            session.flush()
            run_id = run.id
            started_at = run.started_at

        observations = {
            observation.target_id: observation
            for package in packages for observation in package.observations
        }
        projection_by_target_id = {}
        watermark_by_target_id = {}
        with self.sessions() as session:
            for target_id in sorted(observations):
                observation = observations[target_id]
                snapshot = load_target_export_snapshot(
                    session,
                    target_id,
                    measurement_ids=observation.measurement_ids,
                )
                projection_by_target_id[target_id] = projection_sha256(
                    snapshot.projection
                )
                watermark_by_target_id[target_id] = (
                    snapshot.dirty_event_watermark
                )
            for target_id in selection.target_ids:
                watermark_by_target_id.setdefault(
                    target_id, export_dirty_watermark(session, target_id),
                )

        previous_inputs = _previous_export_inputs(output_dir)
        items: list[dict[str, object]] = []
        tasks = []
        package_by_target_id = {}
        for package in packages:
            package_dir = output_dir / package.directory_name
            package_dir.mkdir(parents=True, exist_ok=True)
            for observation in package.observations:
                target_id = observation.target_id
                sdbid = observation.sdbid
                package_by_target_id[target_id] = package.package_id
                output = package_dir / f"{sdbid}-rawphot.txt"
                projection_digest = projection_by_target_id[target_id]
                relative_output = str(output.relative_to(output_dir))
                previous = previous_inputs.get(relative_output)
                status, digest, error = "exported", None, None
                if (
                    not force
                    and previous is not None
                    and previous.get("projection_sha256") == projection_digest
                    and output.is_file()
                ):
                    digest = self._sha256(output)
                    if digest == previous.get("sha256"):
                        status = "skipped"
                if status == "exported":
                    tasks.append((
                        target_id,
                        sdbid,
                        output,
                        projection_digest,
                        observation.measurement_ids,
                    ))
                    continue
                items.append({
                    "target_id": target_id,
                    "sdbid": sdbid,
                    "status": status,
                    "output": str(output),
                    "sha256": digest,
                    "projection_sha256": projection_digest,
                    "error": error,
                })

        database = str(self.sessions.kw["bind"].url.database)
        worker_tasks = [
            (
                database,
                target_id,
                sdbid,
                str(output),
                projection_digest,
                measurement_ids,
            )
            for (
                target_id,
                sdbid,
                output,
                projection_digest,
                measurement_ids,
            ) in tasks
        ]
        if self.workers == 1 or len(worker_tasks) < 2:
            exported_items = map(_export_target_task, worker_tasks)
        else:
            executor = ProcessPoolExecutor(max_workers=min(
                self.workers, len(worker_tasks),
            ))
            exported_items = executor.map(_export_target_task, worker_tasks)
        try:
            items.extend(self.reporter.iter(
                exported_items,
                desc=f"Exporting {selection.kind} selection",
                total=len(worker_tasks),
                unit="target",
            ))
        finally:
            if self.workers > 1 and len(worker_tasks) >= 2:
                executor.shutdown()

        items.sort(key=lambda item: item["sdbid"])
        for item in items:
            if item.get("dirty_event_watermark") is not None:
                watermark_by_target_id[item["target_id"]] = item[
                    "dirty_event_watermark"
                ]
        for item in items:
            with self.sessions.begin() as session:
                session.add(ExportItem(
                    run_id=run_id,
                    target_id=item["target_id"],
                    package_id=package_by_target_id[item["target_id"]],
                    status=item["status"],
                    output_path=item["output"],
                    sha256=item["sha256"],
                    error=item["error"],
                ))

        completed_at = datetime.now(timezone.utc)
        items_by_target_id = {item["target_id"]: item for item in items}
        package_rows = []
        for package in packages:
            package_items = [
                items_by_target_id[target.target_id]
                for target in package.observations
            ]
            package_failed = any(
                item["status"] == "failed" for item in package_items
            )
            package_dir = output_dir / package.directory_name
            joint_fit_path = package_dir / "joint-fit.yml"
            joint_fit = None
            if not package_failed:
                expected_rawphot_names = {
                    f"{observation.sdbid}-rawphot.txt"
                    for observation in package.observations
                }
                for stale_path in package_dir.glob("*-rawphot.txt"):
                    if stale_path.name not in expected_rawphot_names:
                        stale_path.unlink()
                mappings = {
                    observation.sdbid: observation.contributor_sdbids
                    for observation in package.observations
                    if observation.contributor_sdbids != (
                        observation.sdbid,
                    )
                }
                if mappings:
                    write_joint_fit(
                        joint_fit_path,
                        JointFitDefinition(observations=mappings),
                    )
                    joint_fit = {
                        "path": str(joint_fit_path.relative_to(output_dir)),
                        "sha256": self._sha256(joint_fit_path),
                    }
                else:
                    joint_fit_path.unlink(missing_ok=True)
                (package_dir / "joint-fit.json").unlink(missing_ok=True)
            package_rows.append({
                "package_id": package.package_id,
                "directory": package.directory_name,
                "primary_sdbid": package.primary_sdbid,
                "selected_sdbids": list(package.selected_sdbids),
                "status": "partial" if package_failed else "completed",
                "model_sdbids": list(package.model_sdbids),
                "observation_sdbids": [
                    observation.sdbid
                    for observation in package.observations
                ],
                "joint_fit": joint_fit,
            })

        failed = sum(item["status"] == "failed" for item in items)
        manifest_path = output_dir / f"export-{run_id}-manifest.json"
        manifest_items = []
        for item in items:
            value = dict(item)
            value.pop("dirty_event_watermark", None)
            value["output"] = str(Path(item["output"]).relative_to(output_dir))
            value["package_id"] = package_by_target_id[item["target_id"]]
            manifest_items.append(value)
        manifest = {
            "schema": "sdb-fit-package-export",
            "schema_version": 2,
            "run_id": run_id,
            "selection": selection.as_dict(),
            "database_revision": revision,
            "status": "partial" if failed else "completed",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "selected_target_count": len(selection.target_ids),
            "target_count": len(items),
            "package_count": len(packages),
            "exported": sum(item["status"] == "exported" for item in items),
            "skipped": sum(item["status"] == "skipped" for item in items),
            "failed": failed,
            "packages": package_rows,
            "items": manifest_items,
        }
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(manifest_path)

        successful_package_ids = {
            row["package_id"] for row in package_rows
            if row["status"] == "completed"
        }
        affected_packages_by_target_id: dict[int, set[str]] = {}
        for package in packages:
            for target in package.observations:
                affected_packages_by_target_id.setdefault(
                    target.target_id, set(),
                ).add(package.package_id)
            for selected_sdbid in package.selected_sdbids:
                try:
                    selected_index = selection.sdbids.index(selected_sdbid)
                except ValueError:
                    continue
                target_id = selection.target_ids[selected_index]
                affected_packages_by_target_id.setdefault(
                    target_id, set(),
                ).add(package.package_id)
        with self.sessions.begin() as session:
            for target_id, watermark in watermark_by_target_id.items():
                affected = affected_packages_by_target_id.get(target_id, set())
                if (
                    watermark is not None
                    and affected
                    and affected.issubset(successful_package_ids)
                ):
                    mark_exported_through(session, target_id, watermark)
            stored = session.get(ExportRun, run_id)
            stored.status = manifest["status"]
            stored.manifest_path = str(manifest_path)
            stored.completed_at = completed_at
        return PackageExportSummary(
            run_id=run_id,
            selection_kind=selection.kind,
            selection_value=selection.value,
            selected_target_count=len(selection.target_ids),
            target_count=len(items),
            package_count=len(packages),
            exported=manifest["exported"],
            skipped=manifest["skipped"],
            failed=failed,
            manifest=str(manifest_path),
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _fit_packages(report: dict[str, object]) -> list[_FitPackage]:
    """Select fitting groups reached by the selection and give them owners."""
    targets = {row["target_id"]: row for row in report["targets"]}
    selected_ids = {
        row["target_id"] for row in report["targets"] if row["selected"]
    }
    selected_system_ids = {
        membership["system_id"]
        for target_id in selected_ids
        for membership in targets[target_id]["systems"]
    }
    groups = []
    for group in report["groups"]:
        group_system_ids = {
            membership["system_id"]
            for target_id in group["target_ids"]
            for membership in targets[target_id]["systems"]
        }
        if not (
            selected_ids.intersection(group["target_ids"])
            or selected_ids.intersection(group["composite_scope_target_ids"])
            or selected_system_ids.intersection(group_system_ids)
        ):
            continue
        candidate = _package_directory_candidate(group, targets)
        groups.append((group, candidate))

    uncovered = []
    for target_id in selected_ids:
        selected_systems = {
            membership["system_id"]
            for membership in targets[target_id]["systems"]
        }
        covered = any(
            target_id in group["target_ids"]
            or target_id in group["composite_scope_target_ids"]
            or selected_systems.intersection({
                membership["system_id"]
                for group_target_id in group["target_ids"]
                for membership in targets[group_target_id]["systems"]
            })
            for group, _candidate in groups
        )
        if not covered:
            uncovered.append(targets[target_id]["sdbid"])
    if uncovered:
        raise ValueError(
            "selected targets do not resolve to physical fitting groups: "
            + ", ".join(sorted(uncovered))
        )

    candidate_counts = Counter(candidate for _group, candidate in groups)
    packages = []
    for group, candidate in groups:
        directory_name = (
            candidate if candidate_counts[candidate] == 1
            else sorted(group["sdbids"])[0]
        )
        observations = _package_observations(report, group, targets)
        group_system_ids = {
            membership["system_id"]
            for target_id in group["target_ids"]
            for membership in targets[target_id]["systems"]
        }
        package_selected_sdbids = tuple(sorted(
            targets[target_id]["sdbid"]
            for target_id in selected_ids
            if target_id in group["target_ids"]
            or target_id in group["composite_scope_target_ids"]
            or group_system_ids.intersection({
                membership["system_id"]
                for membership in targets[target_id]["systems"]
            })
        ))
        packages.append(_FitPackage(
            package_id=group["group_id"],
            directory_name=directory_name,
            primary_sdbid=directory_name,
            selected_sdbids=package_selected_sdbids,
            model_sdbids=tuple(sorted(group["sdbids"])),
            observations=observations,
        ))
    return sorted(packages, key=lambda package: package.directory_name)


def fit_package_target_ids(report: dict[str, object]) -> set[int]:
    """Return rawphot observation targets reached by a selection."""
    return {
        observation.target_id
        for package in _fit_packages(report)
        for observation in package.observations
    }


def _package_observations(
    report: dict[str, object],
    group: dict[str, object],
    targets: dict[int, dict[str, object]],
) -> tuple[_Observation, ...]:
    """Partition a fitting group's measurements by observation scope."""

    group_target_ids = set(group["target_ids"])
    measurement_ids = {
        *group["fit_measurement_ids"],
        *group["context_measurement_ids"],
    }
    measurements = {
        row["measurement_id"]: row for row in report["measurements"]
        if row["measurement_id"] in measurement_ids
    }
    rows_by_observation: dict[int, set[int]] = {
        target_id: set() for target_id in group_target_ids
    }
    contributors_by_observation: dict[int, tuple[int, ...]] = {
        target_id: (target_id,) for target_id in group_target_ids
    }

    for measurement_id in sorted(measurements):
        row = measurements[measurement_id]
        if not row["current_encounter"]:
            continue
        contributors = tuple(sorted(
            set(row["active_model_contributor_ids"]) & group_target_ids
        ))
        if not contributors:
            continue
        scope_ids = tuple(sorted(set(row["composite_scope_target_ids"])))
        if len(scope_ids) > 1:
            raise ValueError(
                f"measurement {measurement_id} has several observation "
                "scopes; assign one composite scope"
            )
        if scope_ids:
            observation_id = scope_ids[0]
            scope = targets.get(observation_id)
            if scope is None or scope["role"] != "composite":
                raise ValueError(
                    f"measurement {measurement_id} observation scope is not "
                    "a composite target"
                )
        elif len(contributors) == 1:
            observation_id = contributors[0]
        else:
            raise ValueError(
                f"measurement {measurement_id} has several contributors but "
                "no composite observation scope"
            )

        previous = contributors_by_observation.get(observation_id)
        if previous is not None and previous != contributors:
            raise ValueError(
                f"observation {targets[observation_id]['sdbid']} has "
                "inconsistent contributor sets"
            )
        contributors_by_observation[observation_id] = contributors
        rows_by_observation.setdefault(observation_id, set()).add(
            measurement_id
        )

    observations = []
    for target_id in sorted(
        rows_by_observation,
        key=lambda value: targets[value]["sdbid"],
    ):
        observations.append(_Observation(
            target_id=target_id,
            sdbid=targets[target_id]["sdbid"],
            measurement_ids=tuple(sorted(rows_by_observation[target_id])),
            contributor_sdbids=tuple(
                targets[value]["sdbid"]
                for value in contributors_by_observation[target_id]
            ),
        ))
    return tuple(observations)


def _package_directory_candidate(
    group: dict[str, object],
    targets: dict[int, dict[str, object]],
) -> str:
    if len(group["sdbids"]) == 1:
        return group["sdbids"][0]
    composite_candidates = set(group["composite_scope_sdbids"])
    group_system_ids = {
        membership["system_id"]
        for target_id in group["target_ids"]
        for membership in targets[target_id]["systems"]
    }
    for row in targets.values():
        if row["role"] != "composite":
            continue
        if any(
            membership["system_id"] in group_system_ids
            and membership["primary"]
            for membership in row["systems"]
        ):
            composite_candidates.add(row["sdbid"])
    if composite_candidates:
        return sorted(composite_candidates)[0]
    return sorted(group["sdbids"])[0]


def _previous_export_inputs(
    output_dir: Path,
) -> dict[str, dict[str, object]]:
    """Find the newest recorded state for each output in run manifests."""

    result: dict[str, dict[str, object]] = {}
    manifests = sorted(
        output_dir.glob("export-*-manifest.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in manifests:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("schema") != "sdb-fit-package-export":
            continue
        rows = payload.get("items")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if (
                isinstance(row, dict)
                and isinstance(row.get("output"), str)
                and row.get("status") != "failed"
            ):
                result.setdefault(row["output"], row)
    return result


def _export_target_task(
    task: tuple[str, int, str, str, str, tuple[int, ...]],
) -> dict[str, object]:
    (
        database,
        target_id,
        sdbid,
        output_value,
        projection_digest,
        measurement_ids,
    ) = task
    output = Path(output_value)
    try:
        sessions = make_session_factory(database)
        with sessions() as session:
            snapshot = load_target_export_snapshot(
                session,
                target_id,
                measurement_ids=measurement_ids,
            )
        write_ipac_atomic(snapshot.projection, output)
        digest = PackageExportService._sha256(output)
        projection_digest = projection_sha256(snapshot.projection)
        dirty_event_watermark = snapshot.dirty_event_watermark
        status, error = "exported", None
    except Exception as exc:
        digest, dirty_event_watermark = None, None
        status, error = "failed", str(exc)
    return {
        "target_id": target_id,
        "sdbid": sdbid,
        "status": status,
        "output": str(output),
        "sha256": digest,
        "projection_sha256": projection_digest,
        "dirty_event_watermark": dirty_event_watermark,
        "error": error,
    }
