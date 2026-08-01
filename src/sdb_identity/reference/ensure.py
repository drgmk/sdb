"""Ensure configured whole-catalog reference snapshots are present and fresh."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def ensure_reference_snapshots(
    store: Any,
    providers: Iterable[str],
    *,
    cache_path: str | Path | None,
    max_age_days: float,
    check_only: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    if max_age_days <= 0:
        raise ValueError("max_age_days must be positive")
    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(days=max_age_days)
    rows = []
    for provider in providers:
        snapshot = store.current_snapshot(provider)
        if snapshot is None:
            state = "missing"
        else:
            retrieved_at = snapshot.retrieved_at
            if retrieved_at.tzinfo is None:
                retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
            state = "stale" if retrieved_at < cutoff else "current"

        row: dict[str, object] = {
            "provider": provider,
            "state_before": state,
            "snapshot_id": None if snapshot is None else snapshot.id,
            "retrieved_at": (
                None if snapshot is None else snapshot.retrieved_at.isoformat()
            ),
            "action": "none",
        }
        if state != "current" and not check_only:
            result = store.fetch(
                provider,
                cache_path=cache_path,
                refresh_cache=state == "stale",
            )
            row.update({
                "action": "checked" if result.unchanged else "fetched",
                "snapshot_id": result.snapshot_id,
                "content_sha256": result.content_sha256,
                "row_count": result.row_count,
                "unchanged": result.unchanged,
            })
        rows.append(row)

    counts = {
        state: sum(row["state_before"] == state for row in rows)
        for state in ("current", "missing", "stale")
    }
    counts.update({
        "fetched": sum(row["action"] == "fetched" for row in rows),
        "checked": sum(row["action"] == "checked" for row in rows),
    })
    return {
        "mode": "check" if check_only else "ensure",
        "max_age_days": max_age_days,
        "summary": counts,
        "providers": rows,
    }
