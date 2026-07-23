from __future__ import annotations

from collections import Counter

from sqlalchemy.orm import Session, sessionmaker

from .assignment_proposals import measurement_assignment_proposals
from .dirty import find_target
from .photometry import assign_measurement_target
from .progress import NULL_PROGRESS, ProgressReporter
from .samples import SampleService


def apply_measurement_assignment_proposals(
    session_factory: sessionmaker[Session],
    *,
    target_reference: str | int | None = None,
    sample: str | None = None,
    apply: bool = False,
    actor: str | None = None,
    reason: str = "accepted high-confidence automatic assignment proposal",
    reporter: ProgressReporter | None = None,
) -> dict[str, object]:
    """Preview or persist conservative high-confidence assignment proposals.

    Dry-run is the default. Applying never removes current assignments and
    writes only missing target/role pairs through the normal audited assignment
    service. The same canonical measurement may be encountered through several
    targets; inconsistent proposal signatures are retained as review items.
    """
    if (target_reference is None) == (sample is None):
        raise ValueError("specify exactly one target or --sample")
    if apply and not str(actor or "").strip():
        raise ValueError("--actor is required with --apply")
    if not reason.strip():
        raise ValueError("reason is required")
    reporter = reporter or NULL_PROGRESS
    references = _references(
        session_factory, target_reference=target_reference, sample=sample
    )

    by_measurement: dict[int, list[dict[str, object]]] = {}
    for reference in reporter.iter(
        references,
        desc="Evaluating photometry proposals",
        total=len(references),
        unit="target",
    ):
        for proposal in measurement_assignment_proposals(session_factory, reference):
            by_measurement.setdefault(int(proposal["measurement_id"]), []).append(
                proposal
            )

    counts: Counter[str] = Counter()
    items = []
    for measurement_id in sorted(by_measurement):
        proposals = by_measurement[measurement_id]
        signatures = {_proposal_signature(value) for value in proposals}
        proposal = proposals[0]
        base = {
            "measurement_id": measurement_id,
            "provider": proposal["provider"],
            "source_id": proposal["source_id"],
            "band": proposal["band"],
            "proposal_confidence": proposal["proposal_confidence"],
            "proposal_reason": proposal["proposal_reason"],
            "measurement_excluded": bool(proposal.get("excluded")),
            "encountered_from_targets": sorted({
                str(value["origin_sdbid"]) for value in proposals
            }),
        }
        if len(signatures) != 1:
            items.append({
                **base,
                "status": "skipped",
                "skip_reason": "inconsistent_system_proposals",
                "proposed_assignments": [],
            })
            counts["skipped_inconsistent_system_proposals"] += 1
            continue
        proposed = proposal.get("proposed_assignments") or []
        current = proposal.get("current_assignments") or []
        proposed_keys = {
            (int(value["target_id"]), str(value["role"])) for value in proposed
        }
        current_keys = {
            (int(value["target_id"]), str(value["role"])) for value in current
        }
        skip_reason = None
        if proposal.get("proposal_confidence") != "high":
            skip_reason = "not_high_confidence"
        elif not proposed_keys:
            skip_reason = "no_proposed_assignments"
        elif current_keys - proposed_keys:
            skip_reason = "conflicting_current_assignments"
        if skip_reason is not None:
            items.append({
                **base,
                "status": "skipped",
                "skip_reason": skip_reason,
                "proposed_assignments": proposed,
                "current_assignments": current,
            })
            counts[f"skipped_{skip_reason}"] += 1
            continue

        missing = [
            value for value in proposed
            if (int(value["target_id"]), str(value["role"])) not in current_keys
        ]
        if not missing:
            items.append({
                **base,
                "status": "already_current",
                "proposed_assignments": proposed,
            })
            counts["already_current_measurements"] += 1
            counts["already_current_assignments"] += len(proposed)
            continue

        status = "planned"
        if apply:
            audit_reason = f"{reason.strip()}; {proposal['proposal_reason']}"
            for value in missing:
                assign_measurement_target(
                    session_factory,
                    measurement_id,
                    int(value["target_id"]),
                    role=str(value["role"]),
                    method="automatic_proposal",
                    actor=str(actor).strip(),
                    reason=audit_reason,
                )
            status = "applied"
        items.append({
            **base,
            "status": status,
            "assignments": missing,
            "already_current_assignments": len(proposed) - len(missing),
        })
        counts[f"{status}_measurements"] += 1
        counts[f"{status}_assignments"] += len(missing)

    skipped = sum(
        value for key, value in counts.items() if key.startswith("skipped_")
    )
    return {
        "mode": "apply" if apply else "dry_run",
        "selection": {
            "target": None if target_reference is None else str(target_reference),
            "sample": sample,
        },
        "targets_evaluated": len(references),
        "measurements_evaluated": len(by_measurement),
        "summary": {
            **dict(sorted(counts.items())),
            "skipped_measurements": skipped,
        },
        "items": items,
        "notes": [
            "high-confidence proposals are eligible even when the measurement is excluded",
            "assignment never changes provider exclusion or a manual include/exclude override",
            "current assignments are never removed or replaced automatically",
            "legacy per-target export behavior is unchanged",
        ],
    }


def _references(
    session_factory: sessionmaker[Session],
    *,
    target_reference: str | int | None,
    sample: str | None,
) -> list[str]:
    if sample is not None:
        return [value.sdbid for value in SampleService(session_factory).members(sample)]
    with session_factory() as session:
        target = find_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        return [target.sdbid]


def _proposal_signature(proposal: dict[str, object]) -> tuple[object, ...]:
    assignments = tuple(sorted(
        (int(value["target_id"]), str(value["role"]))
        for value in proposal.get("proposed_assignments") or []
    ))
    current = tuple(sorted(
        (int(value["target_id"]), str(value["role"]))
        for value in proposal.get("current_assignments") or []
    ))
    return (
        bool(proposal.get("excluded")),
        str(proposal.get("proposal_confidence")),
        assignments,
        current,
    )
