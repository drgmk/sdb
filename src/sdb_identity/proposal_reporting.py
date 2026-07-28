"""Operator-oriented summaries for measurement assignment proposals."""

from __future__ import annotations

from collections import Counter
from typing import Iterable


def proposal_summary_report(
    proposals: Iterable[dict[str, object]],
    *,
    target: str,
    include_details: bool = False,
) -> dict[str, object]:
    rows = list(proposals)
    provider_counts = Counter(str(row["provider"]) for row in rows)
    confidence_counts = Counter(
        str(row["proposal_confidence"]) for row in rows
    )
    comparison_counts = Counter(
        str(row["comparison_to_current"]) for row in rows
    )
    review_rows = [
        row for row in rows
        if row.get("proposal_confidence") != "high"
        or row.get("comparison_to_current") == "review_required"
    ]
    result: dict[str, object] = {
        "selection": {"target": target},
        "summary": {
            "measurements": len(rows),
            "providers": dict(sorted(provider_counts.items())),
            "confidence": dict(sorted(confidence_counts.items())),
            "comparison_to_current": dict(sorted(comparison_counts.items())),
            "review_required_measurements": len(review_rows),
        },
        "targets_requiring_review": sorted({
            str(row.get("origin_sdbid") or target) for row in review_rows
        }),
    }
    if include_details:
        result["items"] = rows
    else:
        result["notes"] = [
            "use --details for per-measurement proposals",
        ]
    return result


def without_proposal_items(
    result: dict[str, object],
    *,
    include_details: bool,
) -> dict[str, object]:
    if include_details:
        return result
    return {
        key: value for key, value in result.items() if key != "items"
    } | {
        "detail_hint": "use --details for per-measurement proposal results",
    }
