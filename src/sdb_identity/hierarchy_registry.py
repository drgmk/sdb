"""Registry of structural hierarchy evidence sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .hierarchy_ccdm import parse_fixed_width as parse_ccdm_fixed_width
from .hierarchy_records import ParsedHierarchyRecord
from .hierarchy_wds import parse_fixed_width as parse_wds_fixed_width


@dataclass(frozen=True)
class HierarchySourceDefinition:
    key: str
    display_name: str
    catalog: str
    main_table_aliases: frozenset[str]
    fixed_width_parser: Callable[[str], ParsedHierarchyRecord | None]
    identifier_policy: str
    position_policy: str
    graph_capable: bool


HIERARCHY_SOURCES = {
    definition.key: definition
    for definition in (
        HierarchySourceDefinition(
            key="wds",
            display_name="Washington Double Star Catalog",
            catalog="B/wds",
            main_table_aliases=frozenset({"b/wds/wds", "b_wds_wds", "wds"}),
            fixed_width_parser=parse_wds_fixed_width,
            identifier_policy="WDS coordinate identifier plus discoverer and component pair.",
            position_policy="Catalog primary/reference position with last-measure pair geometry.",
            graph_capable=True,
        ),
        HierarchySourceDefinition(
            key="ccdm",
            display_name="Catalogue of Components of Double and Multiple Stars",
            catalog="I/274",
            main_table_aliases=frozenset({"i/274/ccdm", "i_274_ccdm", "ccdm"}),
            fixed_width_parser=parse_ccdm_fixed_width,
            identifier_policy="CCDM coordinate identifier and component columns.",
            position_policy="Coordinate identifier refined by dRAs/dDEs remainder columns.",
            graph_capable=False,
        ),
    )
}


def hierarchy_source(key: str) -> HierarchySourceDefinition:
    try:
        return HIERARCHY_SOURCES[key]
    except KeyError as error:
        raise ValueError(f"unsupported hierarchy provider: {key}") from error
