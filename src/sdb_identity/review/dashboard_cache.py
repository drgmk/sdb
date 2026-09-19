"""Process-local queue index for a long-running review server."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Iterable

from sqlalchemy.orm import Session, sessionmaker

from ..vocabulary import INACTIVE_TARGET_STATES, review_priority_rank
from .dashboard import review_dashboard_report, review_dashboard_summary


class ReviewDashboardCache:
    """Cache the expensive all-target queue while target workspaces stay live.

    Operator writes refresh only affected target rows.  Changes made by another
    process are detected when the queue page is requested and cause one full
    rebuild, so leaving the server running does not silently freeze its view of
    the database.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        sample: str | None,
        all_targets: bool,
        catalog_providers: tuple[str, ...] | None,
    ) -> None:
        self.session_factory = session_factory
        self.sample = sample
        self.all_targets = all_targets
        self.catalog_providers = catalog_providers
        self._report: dict[str, object] | None = None
        self._fingerprint: tuple[tuple[int, int] | None, ...] | None = None
        self._lock = RLock()

    def get(self, *, detect_external_changes: bool = False) -> dict[str, object]:
        with self._lock:
            fingerprint = self._database_fingerprint()
            if (
                self._report is None
                or (
                    detect_external_changes
                    and fingerprint != self._fingerprint
                )
            ):
                self._report = self._build()
                self._fingerprint = self._database_fingerprint()
            return self._report

    def invalidate(self) -> None:
        with self._lock:
            self._report = None
            self._fingerprint = None

    def refresh_related(
        self,
        *,
        target_references: Iterable[str] = (),
        detection_ids: Iterable[int] = (),
        measurement_ids: Iterable[int] = (),
    ) -> None:
        """Refresh cached rows touched by a successful operator write."""

        with self._lock:
            if self._report is None:
                return
            references = {str(value) for value in target_references if value}
            selected_detections = {int(value) for value in detection_ids}
            selected_measurements = {int(value) for value in measurement_ids}
            for row in self._report["rows"]:
                if any(
                    int(detection["detection_id"]) in selected_detections
                    or bool(
                        set(int(value) for value in detection["measurement_ids"])
                        & selected_measurements
                    )
                    for detection in row["detections"]
                ):
                    references.add(str(row["sdbid"]))
            for reference in sorted(references):
                self._refresh_target(reference)
            self._fingerprint = self._database_fingerprint()

    def _refresh_target(self, reference: str) -> None:
        if self._report is None:
            return
        refreshed = review_dashboard_report(
            self.session_factory,
            target_reference=reference,
            catalog_providers=self.catalog_providers,
        )
        new_row = refreshed["rows"][0] if refreshed["rows"] else None
        refreshed_sdbid = (
            reference if new_row is None else str(new_row["sdbid"])
        )
        old_rows = list(self._report["rows"])
        old_index = next((
            index for index, row in enumerate(old_rows)
            if row["sdbid"] == refreshed_sdbid
        ), None)
        include = new_row is not None and (
            (self.all_targets and new_row["state"] not in INACTIVE_TARGET_STATES)
            or (self.sample is not None and old_index is not None)
        )
        if old_index is not None:
            if include:
                old_rows[old_index] = new_row
            else:
                old_rows.pop(old_index)
        elif include:
            old_rows.append(new_row)
        old_rows.sort(key=lambda row: (
            -review_priority_rank(str(row["priority"])), str(row["sdbid"]),
        ))
        selection = {
            **self._report["selection"],
            "selected_sdbids": [str(row["sdbid"]) for row in old_rows],
        }
        self._report = {
            **self._report,
            "selection": selection,
            "rows": old_rows,
            "summary": review_dashboard_summary(old_rows),
        }

    def _build(self) -> dict[str, object]:
        return review_dashboard_report(
            self.session_factory,
            sample=self.sample,
            all_targets=self.all_targets,
            catalog_providers=self.catalog_providers,
        )

    def _database_fingerprint(self) -> tuple[tuple[int, int] | None, ...] | None:
        bind = self.session_factory.kw.get("bind")
        database = None if bind is None else bind.url.database
        if not database or database == ":memory:":
            return None
        path = Path(database)
        return tuple(self._file_state(value) for value in (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
        ))

    @staticmethod
    def _file_state(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size
