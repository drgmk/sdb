"""Pure scoring and automatic-selection policy for catalog candidates."""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..astrometry import angular_separation_arcsec
from .types import CatalogAdapter, CatalogCandidate, CatalogQueryContext


@dataclass(frozen=True)
class ScoredCatalogCandidate:
    candidate: CatalogCandidate
    separation_arcsec: float
    score: float


@dataclass(frozen=True)
class CatalogMatch:
    candidates: tuple[ScoredCatalogCandidate, ...]
    selected_index: int | None


def match_catalog_candidates(
    adapter: CatalogAdapter,
    context: CatalogQueryContext,
    candidates: list[CatalogCandidate],
    *,
    acceptance_score: float,
    acceptance_margin: float,
    score_scale_arcsec: float,
) -> CatalogMatch:
    adapter_acceptance_score = float(
        getattr(adapter, "acceptance_score", acceptance_score)
    )
    adapter_acceptance_margin = float(
        getattr(adapter, "acceptance_margin", acceptance_margin)
    )
    scored = []
    for candidate in candidates:
        if hasattr(adapter, "candidate_separation"):
            separation = adapter.candidate_separation(context, candidate)
        else:
            separation = angular_separation_arcsec(
                context.astrometry,
                candidate.astrometry,
                epoch=adapter.query_epoch,
            )
        score = (
            adapter.score_candidate(context, candidate, separation)
            if hasattr(adapter, "score_candidate")
            else math.exp(-0.5 * (separation / score_scale_arcsec) ** 2)
        )
        scored.append(ScoredCatalogCandidate(candidate, separation, score))
    scored.sort(key=lambda item: item.score, reverse=True)
    selected_index = None
    if scored:
        runner_up = scored[1].score if len(scored) > 1 else 0.0
        if (
            scored[0].score >= adapter_acceptance_score
            and (
                len(scored) == 1
                or scored[0].score - runner_up >= adapter_acceptance_margin
            )
        ):
            selected_index = 0
    return CatalogMatch(tuple(scored), selected_index)
