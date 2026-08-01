from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from ..models.hierarchy import (
    HierarchySource,
    StructuralEdge,
    TargetSystem,
    TargetSystemMember,
)
from .graph import (
    HierarchyGraphDeriveResult,
    HierarchyGraphDiagnosticRow,
    HierarchyGraphEdgeRow,
    HierarchyGraphOverrideResult,
)
from .graph_service import HierarchyGraphService
from .matching import (
    HierarchyMatchActionResult,
    HierarchyMatchResult,
    HierarchyMatchReviewRow,
    HierarchyMatchingService,
    HierarchyTargetMatchResult,
)
from .reporting import HierarchyReportingService
from .sources import (
    HierarchyImportResult,
    HierarchyPruneResult,
    HierarchySourceService,
)
from .structure import (
    HierarchyStatus,
    HierarchyStructureService,
    RelationshipSummary as RelationshipSummary,
    SystemMember as SystemMember,
)
from .system_context import HierarchySystemContextService
from .target_context import (
    HierarchyTargetContextService,
)
from ..snapshots import SnapshotClient


class HierarchyService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def create_system(
        self,
        name: str,
        *,
        primary: str | int | None = None,
        source: str = "manual",
        note: str | None = None,
    ) -> TargetSystem:
        return HierarchyStructureService(self.session_factory).create_system(
            name, primary=primary, source=source, note=note,
        )

    def add_member(
        self,
        system_name: str,
        target_reference: str | int,
        *,
        component_label: str | None = None,
        source: str = "manual",
    ) -> TargetSystemMember:
        return HierarchyStructureService(self.session_factory).add_member(
            system_name,
            target_reference,
            component_label=component_label,
            source=source,
        )

    def add_relationship(
        self,
        *,
        relationship_type: str,
        primary: str | int | None = None,
        secondary: str | int | None = None,
        parent: str | int | None = None,
        child: str | int | None = None,
        system: str | None = None,
        component: str | None = None,
        source: str = "manual",
        separation_arcsec: float | None = None,
        pa_deg: float | None = None,
        relation_epoch: float | None = None,
        confidence: str = "manual",
        status: str = "current",
        actor: str | None = None,
        reason: str = "",
    ) -> StructuralEdge:
        return HierarchyStructureService(self.session_factory).add_relationship(
            relationship_type=relationship_type,
            primary=primary,
            secondary=secondary,
            parent=parent,
            child=child,
            system=system,
            component=component,
            source=source,
            separation_arcsec=separation_arcsec,
            pa_deg=pa_deg,
            relation_epoch=relation_epoch,
            confidence=confidence,
            status=status,
            actor=actor,
            reason=reason,
        )

    def status(self, target_reference: str | int) -> HierarchyStatus:
        return HierarchyStructureService(self.session_factory).status(
            target_reference,
        )

    def target_context(
        self,
        target_reference: str | int,
        *,
        include_diagnostics: bool = True,
    ) -> dict[str, object]:
        return HierarchyTargetContextService(self.session_factory).target_context(
            target_reference, include_diagnostics=include_diagnostics,
        )

    def target_context_summary(
        self, target_reference: str | int,
    ) -> dict[str, object]:
        return HierarchyTargetContextService(
            self.session_factory,
        ).target_context_summary(target_reference)

    def system_context(
        self,
        target_reference: str | int,
        *,
        catalog_providers: Iterable[str] | None = None,
        radius_arcsec: float | None = None,
    ) -> dict[str, object]:
        return HierarchySystemContextService(self.session_factory).system_context(
            target_reference,
            catalog_providers=catalog_providers,
            radius_arcsec=radius_arcsec,
        )

    def photometry_review(
        self,
        target_references: list[str | int],
        *,
        provider: str | None = None,
        blended_only: bool = False,
        review_required: bool = False,
    ) -> list[dict[str, object]]:
        return HierarchyTargetContextService(self.session_factory).photometry_review(
            target_references,
            provider=provider,
            blended_only=blended_only,
            review_required=review_required,
        )

    def review_queue(
        self,
        target_references: list[str | int],
        *,
        provider: str | None = None,
        min_priority: str | None = None,
    ) -> list[dict[str, object]]:
        return HierarchyTargetContextService(self.session_factory).review_queue(
            target_references, provider=provider, min_priority=min_priority,
        )

    def import_snapshot(
        self,
        provider: str,
        path: str | Path,
        *,
        release: str,
        note: str | None = None,
    ) -> HierarchyImportResult:
        return HierarchySourceService(self.session_factory).import_snapshot(
            provider, path, release=release, note=note,
        )

    def fetch_snapshot(
        self,
        provider: str,
        *,
        client: SnapshotClient | None = None,
        cache_path: str | Path | None = None,
        refresh_cache: bool = False,
        release: str | None = None,
        note: str | None = None,
    ) -> HierarchyImportResult:
        return HierarchySourceService(self.session_factory).fetch_snapshot(
            provider,
            client=client,
            cache_path=cache_path,
            refresh_cache=refresh_cache,
            release=release,
            note=note,
        )

    def sources(self, provider: str | None = None) -> tuple[HierarchySource, ...]:
        return HierarchySourceService(self.session_factory).sources(provider)

    def prune_duplicate_sources(
        self, provider: str | None = None,
    ) -> HierarchyPruneResult:
        return HierarchySourceService(
            self.session_factory,
        ).prune_duplicate_sources(provider)

    def summary(
        self, provider: str | None = None, *, source_id: int | None = None,
    ) -> dict[str, object]:
        return HierarchyReportingService(self.session_factory).summary(
            provider, source_id=source_id,
        )

    def match_records(
        self,
        provider: str,
        *,
        source_id: int | None = None,
        radius_arcsec: float = 30.0,
    ) -> HierarchyMatchResult:
        return HierarchyMatchingService(self.session_factory).match_records(
            provider, source_id=source_id, radius_arcsec=radius_arcsec,
        )

    def match_targets(
        self,
        provider: str,
        target_references: Iterable[str | int],
        *,
        radius_arcsec: float = 30.0,
    ) -> HierarchyTargetMatchResult:
        return HierarchyMatchingService(self.session_factory).match_targets(
            provider, target_references, radius_arcsec=radius_arcsec,
        )

    def derive_graph(
        self,
        provider: str,
        *,
        source_id: int | None = None,
    ) -> HierarchyGraphDeriveResult:
        return HierarchyGraphService(self.session_factory).derive_graph(
            provider, source_id=source_id,
        )

    def graph_edges(
        self,
        *,
        provider: str | None = None,
        native_id: str | None = None,
        target: str | int | None = None,
        source_id: int | None = None,
    ) -> tuple[HierarchyGraphEdgeRow, ...]:
        return HierarchyGraphService(self.session_factory).graph_edges(
            provider=provider,
            native_id=native_id,
            target=target,
            source_id=source_id,
        )

    def graph_diagnostics(
        self,
        *,
        provider: str | None = None,
        source_id: int | None = None,
        native_id: str | None = None,
        limit: int = 100,
        severity: str | None = None,
        issue: str | None = None,
    ) -> tuple[HierarchyGraphDiagnosticRow, ...]:
        return HierarchyGraphService(self.session_factory).graph_diagnostics(
            provider=provider,
            source_id=source_id,
            native_id=native_id,
            limit=limit,
            severity=severity,
            issue=issue,
        )

    def override_graph_edge(
        self,
        *,
        provider: str,
        native_id: str,
        reference_label: str,
        component_label: str,
        actor: str | None,
        reason: str | None = None,
        source_id: int | None = None,
        status: str | None = None,
        relation_type: str | None = None,
        structural_role: str | None = None,
    ) -> HierarchyGraphOverrideResult:
        return HierarchyGraphService(self.session_factory).override_graph_edge(
            provider=provider,
            native_id=native_id,
            reference_label=reference_label,
            component_label=component_label,
            actor=actor,
            reason=reason,
            source_id=source_id,
            status=status,
            relation_type=relation_type,
            structural_role=structural_role,
        )

    def review_matches(
        self, provider: str | None = None,
    ) -> tuple[HierarchyMatchReviewRow, ...]:
        return HierarchyMatchingService(self.session_factory).review_matches(provider)

    def accept_match(
        self,
        candidate_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
        system: str | None = None,
        component_label: str | None = None,
        relationship_type: str = "hierarchy_record",
    ) -> HierarchyMatchActionResult:
        return HierarchyMatchingService(self.session_factory).accept_match(
            candidate_id,
            actor=actor,
            reason=reason,
            system=system,
            component_label=component_label,
            relationship_type=relationship_type,
        )

    def reject_match(
        self,
        candidate_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> HierarchyMatchActionResult:
        return HierarchyMatchingService(self.session_factory).reject_match(
            candidate_id, actor=actor, reason=reason,
        )
