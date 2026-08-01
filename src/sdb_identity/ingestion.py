"""Shared orchestration for target identity, provider coverage, and hierarchy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .hierarchy.matching import HierarchyMatchingService
from .service import AddRequest, AddResult, IdentityService
from .update import UpdateService, UpdateSummary


@dataclass(frozen=True)
class TargetIngestionFollowup:
    update_summary: UpdateSummary | None
    hierarchy_matches: tuple[dict[str, object], ...]


class TargetIngestionPlan:
    """One sequencing boundary shared by interactive and durable imports."""

    def __init__(
        self,
        *,
        identity: IdentityService,
        update: UpdateService | None = None,
        hierarchy: HierarchyMatchingService | None = None,
    ):
        self.identity = identity
        self.update = update
        self.hierarchy = hierarchy

    def identify(
        self,
        request: AddRequest,
        *,
        name_resolution: object | None = None,
        prefetched: bool = False,
    ) -> AddResult:
        if prefetched:
            return self.identity.add(
                request, name_resolution=name_resolution,
            )
        return self.identity.add(request)

    def follow_up(
        self,
        target_sdbids: Iterable[str],
        *,
        providers: Iterable[str],
        hierarchy_radius_arcsec: float = 30.0,
    ) -> TargetIngestionFollowup:
        targets = tuple(dict.fromkeys(
            str(value).strip()
            for value in target_sdbids
            if str(value).strip()
        ))
        selected_providers = tuple(dict.fromkeys(
            str(value).strip()
            for value in providers
            if str(value).strip()
        ))
        if not targets:
            return TargetIngestionFollowup(None, ())
        if selected_providers and self.update is None:
            raise RuntimeError(
                "provider follow-up requires an update service"
            )
        update_summary = (
            None
            if not selected_providers
            else self.update.update_targets(
                targets,
                providers=selected_providers,
                force=False,
            )
        )
        hierarchy_matches = []
        if self.hierarchy is not None:
            for provider in ("wds", "ccdm"):
                hierarchy_matches.append(asdict(
                    self.hierarchy.match_targets(
                        provider,
                        targets,
                        radius_arcsec=hierarchy_radius_arcsec,
                    )
                ))
        return TargetIngestionFollowup(
            update_summary,
            tuple(hierarchy_matches),
        )
