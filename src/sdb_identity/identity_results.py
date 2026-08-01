"""Effective identity-candidate selections derived from decision history."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models.identity import MatchCandidate, MatchDecision, Submission


def effective_identity_candidate_ids(
    session: Session,
    *,
    target_ids: Iterable[int] | None = None,
    submission_ids: Iterable[int] | None = None,
) -> set[int]:
    """Return the selected candidate for each requested submission.

    Automatic and manual acceptances share one append-only history. If a
    later manual acceptance chooses a sibling, that later accepted decision
    becomes the submission's sole effective selection.
    """

    targets = (
        None
        if target_ids is None
        else tuple(dict.fromkeys(int(value) for value in target_ids))
    )
    submissions = (
        None
        if submission_ids is None
        else tuple(dict.fromkeys(int(value) for value in submission_ids))
    )
    if targets == () or submissions == ():
        return set()
    query = (
        select(MatchDecision, MatchCandidate.submission_id)
        .join(
            MatchCandidate,
            MatchCandidate.id == MatchDecision.candidate_id,
        )
        .where(MatchDecision.decision == "accepted")
        .order_by(MatchDecision.id)
    )
    if targets is not None:
        query = query.join(
            Submission,
            Submission.id == MatchCandidate.submission_id,
        ).where(Submission.target_id.in_(targets))
    if submissions is not None:
        query = query.where(
            MatchCandidate.submission_id.in_(submissions)
        )
    selected_by_submission: dict[int, int] = {}
    for decision, submission_id in session.execute(query):
        selected_by_submission[submission_id] = decision.candidate_id
    return set(selected_by_submission.values())
