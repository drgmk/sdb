"""Read-side projection for possible duplicate target imports."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models.identity import Target, TargetDuplicateReview


def target_duplicate_reviews(
    session: Session,
    target_ids: Iterable[int],
) -> dict[int, list[dict[str, object]]]:
    selected = set(int(value) for value in target_ids)
    if not selected:
        return {}
    rows = list(session.scalars(
        select(TargetDuplicateReview).where(
            TargetDuplicateReview.status == "review",
            or_(
                TargetDuplicateReview.target_id.in_(selected),
                TargetDuplicateReview.possible_duplicate_target_id.in_(selected),
            ),
        ).order_by(TargetDuplicateReview.id)
    ))
    target_lookup = {
        target.id: target
        for target in session.scalars(select(Target).where(Target.id.in_({
            target_id
            for row in rows
            for target_id in (row.target_id, row.possible_duplicate_target_id)
        })))
    }
    result: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        for target_id, other_id in (
            (row.target_id, row.possible_duplicate_target_id),
            (row.possible_duplicate_target_id, row.target_id),
        ):
            if target_id not in selected:
                continue
            other = target_lookup.get(other_id)
            result[target_id].append({
                "review_id": row.id,
                "other_target_id": other_id,
                "other_sdbid": None if other is None else other.sdbid,
                "separation_arcsec": row.separation_arcsec,
                "reason": row.reason,
                "imported_target": target_id == row.target_id,
            })
    return dict(result)
