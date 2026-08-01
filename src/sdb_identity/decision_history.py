"""On-demand normalized operator history for one target system.

The domain action tables remain authoritative.  This module deliberately pays
the cost of joining them only when an operator asks for a combined history.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .catalogs.measurements import current_measurement_target_ids
from .models.catalogs import (
    CatalogResultDecision,
    CatalogRetryAction,
    CatalogTargetAssociationAction,
    CatalogDetection,
    NormalizedMeasurement,
)
from .models.curated import CuratedAssociationAction
from .models.hierarchy import (
    HierarchyMatchAction,
    HierarchyMatchCandidate,
    StructuralEdge,
    StructuralEdgeAction,
    TargetLifecycleAction,
    TargetSystemMember,
)
from .models.identity import MatchCandidate, MatchDecision, Submission, Target
from .models.photometry import (
    MeasurementAssociationAction,
    MeasurementEligibilityAction,
)
from .models.samples import Sample, SampleMembershipAction
from .targets import resolve_target


def system_decision_history(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    *,
    include_system: bool = True,
) -> list[dict[str, object]]:
    with session_factory() as session:
        requested = resolve_target(session, target_reference)
        if requested is None:
            raise KeyError(f"target not found: {target_reference}")
        target_ids = _system_target_ids(session, requested.id) if include_system else {requested.id}
        targets = {
            row.id: row.sdbid
            for row in session.scalars(select(Target).where(Target.id.in_(target_ids)))
        }
        rows: list[dict[str, object]] = []

        for action in session.scalars(
            select(TargetLifecycleAction)
            .where(TargetLifecycleAction.target_id.in_(target_ids))
        ):
            rows.append(_row(
                action, "target_lifecycle", f"target:{targets[action.target_id]}",
                f"set role={action.role}, state={action.state}",
            ))

        for action, candidate in session.execute(
            select(MatchDecision, MatchCandidate)
            .join(MatchCandidate, MatchCandidate.id == MatchDecision.candidate_id)
            .join(Submission, Submission.id == MatchCandidate.submission_id)
            .where(
                Submission.target_id.in_(target_ids),
                MatchDecision.method == "manual",
                MatchDecision.actor.is_not(None),
            )
        ):
            rows.append(_row(
                action, "identity_match",
                f"{candidate.provider}:{candidate.source_id}",
                action.decision,
            ))

        for action, sample in session.execute(
            select(SampleMembershipAction, Sample)
            .join(Sample, Sample.id == SampleMembershipAction.sample_id)
            .where(SampleMembershipAction.target_id.in_(target_ids))
        ):
            rows.append(_row(
                action, "sample_membership",
                f"sample:{sample.name}/target:{targets[action.target_id]}",
                action.action,
            ))

        eligibility_actions = list(session.execute(
            select(MeasurementEligibilityAction, NormalizedMeasurement)
            .join(
                NormalizedMeasurement,
                NormalizedMeasurement.id
                == MeasurementEligibilityAction.measurement_id,
            )
        ))
        eligibility_targets = current_measurement_target_ids(
            session,
            [action.measurement_id for action, _measurement in eligibility_actions],
        )
        for action, measurement in eligibility_actions:
            related_target_ids = {
                measurement.target_id,
                *eligibility_targets.get(measurement.id, set()),
            }
            if related_target_ids.isdisjoint(target_ids):
                continue
            rows.append(_row(
                action, "photometry_eligibility",
                (
                    f"measurement:{measurement.id}:"
                    f"{measurement.provider}:{measurement.band}"
                ),
                "exclude" if action.excluded else "include",
            ))

        measurement_actions = session.execute(
            select(MeasurementAssociationAction, NormalizedMeasurement)
            .join(
                NormalizedMeasurement,
                NormalizedMeasurement.id == MeasurementAssociationAction.measurement_id,
            )
            .where(or_(
                MeasurementAssociationAction.target_id.in_(target_ids),
                NormalizedMeasurement.target_id.in_(target_ids),
            ))
        )
        for action, measurement in measurement_actions:
            rows.append(_row(
                action, "measurement_assignment",
                (
                    f"measurement:{measurement.id}/"
                    f"target:{targets.get(action.target_id, action.target_id)}"
                ),
                f"{action.action} {action.role}",
            ))

        for action in session.scalars(
            select(CatalogResultDecision)
            .where(CatalogResultDecision.target_id.in_(target_ids))
        ):
            rows.append(_row(
                action, "catalog_result",
                f"{targets[action.target_id]}:{action.provider}",
                (
                    f"accepted detection {action.accepted_detection_id}"
                    if action.action == "accept_detection"
                    else action.action.replace("_", " ")
                ),
            ))

        for action in session.scalars(
            select(CatalogRetryAction)
            .where(CatalogRetryAction.target_id.in_(target_ids))
        ):
            rows.append(_row(
                action, "catalog_retry",
                f"{targets[action.target_id]}:{action.provider}",
                f"retried run {action.failed_run_id} as {action.retry_run_id}",
            ))

        for action, detection in session.execute(
            select(CatalogTargetAssociationAction, CatalogDetection)
            .join(
                CatalogDetection,
                CatalogDetection.id
                == CatalogTargetAssociationAction.detection_id,
            )
            .where(
                CatalogTargetAssociationAction.target_id.in_(target_ids)
            )
        ):
            rows.append(_row(
                action,
                "catalog_target_association",
                (
                    f"{targets[action.target_id]}:"
                    f"{detection.provider}:{detection.source_id}"
                ),
                action.action,
            ))

        for action in session.scalars(
            select(CuratedAssociationAction)
            .where(CuratedAssociationAction.target_id.in_(target_ids))
        ):
            rows.append(_row(
                action, "curated_association",
                f"{action.dataset}:record:{action.record_no}",
                action.action,
            ))

        for action, candidate in session.execute(
            select(HierarchyMatchAction, HierarchyMatchCandidate)
            .join(
                HierarchyMatchCandidate,
                HierarchyMatchCandidate.id == HierarchyMatchAction.candidate_id,
            )
            .where(HierarchyMatchCandidate.target_id.in_(target_ids))
        ):
            rows.append(_row(
                action, "hierarchy_match",
                f"candidate:{candidate.id}/target:{targets[candidate.target_id]}",
                action.action,
            ))

        structural_actions = session.execute(
            select(StructuralEdgeAction, StructuralEdge)
            .join(StructuralEdge, StructuralEdge.id == StructuralEdgeAction.edge_id)
            .where(or_(
                StructuralEdge.endpoint_a_target_id.in_(target_ids),
                StructuralEdge.endpoint_b_target_id.in_(target_ids),
            ))
        )
        for action, edge in structural_actions:
            rows.append(_row(
                action, "structural_edge",
                f"{edge.source}:{edge.native_id}:{edge.reference_label}->{edge.component_label}",
                action.action,
            ))

        rows.sort(key=lambda row: (str(row["created_at"]), str(row["domain"]), int(row["id"])))
        return rows


def _system_target_ids(session: Session, target_id: int) -> set[int]:
    system_ids = set(session.scalars(
        select(TargetSystemMember.system_id)
        .where(TargetSystemMember.target_id == target_id)
    ))
    if not system_ids:
        return {target_id}
    return {target_id, *session.scalars(
        select(TargetSystemMember.target_id)
        .where(TargetSystemMember.system_id.in_(system_ids))
    )}


def _row(action, domain: str, subject: str, summary: str) -> dict[str, object]:
    return {
        "id": action.id,
        "created_at": action.created_at.isoformat(),
        "domain": domain,
        "subject": subject,
        "action": summary,
        "actor": action.actor,
        "reason": action.reason,
    }
