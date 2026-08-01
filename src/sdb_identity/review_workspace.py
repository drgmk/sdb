"""Transport-independent target workspace projection for review clients."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .photometry.readiness import assignment_readiness_report
from .fitting_groups import fitting_group_report
from .hierarchy.system_context import HierarchySystemContextService
from .models.catalogs import RawCatalogRow
from .review_dashboard import review_dashboard_report


@dataclass(frozen=True)
class TargetWorkspace:
    sdbid: str
    display_name: str | None
    readiness: dict[str, object]
    fitting_graph: dict[str, object]
    system_context: dict[str, object]
    raw_row_detections: dict[int, int]
    navigation: dict[str, object] | None
    simbad_main_ids: dict[str, str]
    catalog_coverage: tuple[dict[str, object], ...]
    target_position: dict[str, object]
    catalog_update_available: bool
    nearby_import_available: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "sdbid": self.sdbid,
            "display_name": self.display_name,
            "readiness": self.readiness,
            "fitting_graph": self.fitting_graph,
            "system_context": self.system_context,
            "raw_row_detections": self.raw_row_detections,
            "navigation": self.navigation,
            "simbad_main_ids": self.simbad_main_ids,
            "catalog_coverage": list(self.catalog_coverage),
            "target_position": self.target_position,
            "capabilities": {
                "catalog_update": self.catalog_update_available,
                "nearby_import": self.nearby_import_available,
            },
        }


def build_target_workspace(
    session_factory: sessionmaker[Session],
    sdbid: str,
    *,
    sample: str | None = None,
    filters: dict[str, str] | None = None,
    position: int | None = None,
    catalog_coverage_providers: tuple[str, ...] | None = None,
    catalog_update_available: bool = False,
    nearby_import_available: bool = False,
) -> TargetWorkspace:
    filters = filters or {}
    readiness = assignment_readiness_report(
        session_factory,
        target_reference=sdbid,
    )
    graph = fitting_group_report(
        session_factory,
        target_reference=sdbid,
    )
    system_context = HierarchySystemContextService(
        session_factory,
    ).system_context(
        sdbid,
        catalog_providers=catalog_coverage_providers,
    )
    simbad_main_ids = dict(system_context.get("simbad_main_id_by_target", {}))
    for relative in system_context.get("simbad_relative_preview", []):
        if (
            relative.get("action") != "context_only"
            and relative.get("matched_sdbid")
            and relative.get("main_id")
        ):
            simbad_main_ids.setdefault(
                str(relative["matched_sdbid"]),
                str(relative["main_id"]),
            )

    navigation = None
    display_name = None
    if sample is not None:
        queue_report = review_dashboard_report(session_factory, sample=sample)
        display_name = next(
            (
                str(row["display_name"])
                for row in queue_report["rows"]
                if row["sdbid"] == sdbid and row.get("display_name")
            ),
            None,
        )
        navigation = queue_navigation(
            queue_report,
            sdbid,
            filters,
            position,
        )
    display_name = display_name or simbad_main_ids.get(sdbid)
    return TargetWorkspace(
        sdbid=sdbid,
        display_name=display_name,
        readiness=readiness,
        fitting_graph=graph,
        system_context=system_context,
        raw_row_detections=raw_row_detection_map(session_factory, graph),
        navigation=navigation,
        simbad_main_ids=simbad_main_ids,
        catalog_coverage=tuple(
            system_context.get("catalog_coverage_by_target", [])
        ),
        target_position=dict(system_context["target"]),
        catalog_update_available=catalog_update_available,
        nearby_import_available=nearby_import_available,
    )


def queue_filters(**values: str) -> dict[str, str]:
    return {
        key: str(value or "").strip()
        for key, value in values.items()
        if str(value or "").strip()
    }


def filtered_queue_rows(
    report: dict[str, object],
    filters: dict[str, str],
) -> list[dict[str, object]]:
    rows = list(report["rows"])
    view = filters.get("view", "actionable")
    if view == "actionable":
        rows = [row for row in rows if row["priority"] != "none"]
    elif view == "clean":
        rows = [row for row in rows if row["priority"] == "none"]
    elif view != "all":
        rows = []
    for key in ("priority", "role", "classification"):
        if key in filters:
            rows = [row for row in rows if str(row[key]) == filters[key]]
    if "provider" in filters:
        rows = [
            row
            for row in rows
            if any(
                str(value["provider"]) == filters["provider"]
                for value in row["providers"]
            )
        ]
    if "search" in filters:
        needle = filters["search"].casefold()
        rows = [
            row
            for row in rows
            if needle
            in " ".join(
                [
                    str(row["sdbid"]),
                    str(row.get("display_name") or ""),
                    str(row["recommended_action"]),
                    str(row["classification"]),
                    *(str(value["provider"]) for value in row["providers"]),
                    *(
                        str(band)
                        for value in row["providers"]
                        for band in value["bands"]
                    ),
                ]
            ).casefold()
        ]
    return rows


def queue_query(filters: dict[str, str], position: int | None = None) -> str:
    values: dict[str, object] = dict(filters)
    if position is not None:
        values["position"] = position
    encoded = urlencode(values)
    return "" if not encoded else f"?{encoded}"


def queue_navigation(
    report: dict[str, object],
    sdbid: str,
    filters: dict[str, str],
    requested_position: int | None,
) -> dict[str, object]:
    rows = filtered_queue_rows(report, filters)
    current_index = next(
        (index for index, row in enumerate(rows) if row["sdbid"] == sdbid),
        None,
    )
    current_present = current_index is not None
    if current_index is None:
        cursor = max(0, int(requested_position or 0))
        previous_index = min(cursor - 1, len(rows) - 1)
        next_index = cursor if cursor < len(rows) else None
    else:
        previous_index = current_index - 1
        next_index = (
            current_index + 1 if current_index + 1 < len(rows) else None
        )

    def target_url(index: int | None) -> str | None:
        if index is None or index < 0 or index >= len(rows):
            return None
        target = str(rows[index]["sdbid"])
        return f"/target/{quote(target)}{queue_query(filters, index)}"

    display_position = (
        current_index + 1
        if current_index is not None
        else min(max(int(requested_position or 0), 0) + 1, len(rows))
    )
    return {
        "filters": filters,
        "back_url": f"/{queue_query(filters)}",
        "previous_url": target_url(previous_index),
        "next_url": target_url(next_index),
        "position": display_position,
        "count": len(rows),
        "current_present": current_present,
    }


def raw_row_detection_map(
    session_factory: sessionmaker[Session],
    graph: dict[str, object],
) -> dict[int, int]:
    detection_ids = {
        int(row["detection_id"]) for row in graph["measurements"]
    }
    if not detection_ids:
        return {}
    with session_factory() as session:
        return {
            int(raw_row_id): int(detection_id)
            for raw_row_id, detection_id in session.execute(
                select(RawCatalogRow.id, RawCatalogRow.detection_id).where(
                    RawCatalogRow.detection_id.in_(detection_ids)
                )
            )
        }
