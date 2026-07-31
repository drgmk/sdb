"""Shared persisted vocabulary for cross-domain SDB state.

Database columns remain strings so exported data and SQL stay readable.  These
``StrEnum`` values give application code one definition for values that cross
several services, projections, CLI commands, and UI routes.
"""

from __future__ import annotations

from enum import StrEnum


class DomainValue(StrEnum):
    """A string value with consistent parsing and CLI-choice support."""

    @classmethod
    def parse(cls, value: str | "DomainValue", field_name: str) -> "DomainValue":
        clean = str(value).strip().lower()
        try:
            return cls(clean)
        except ValueError:
            choices = sorted(item.value for item in cls)
            raise ValueError(f"{field_name} must be one of {choices}") from None

    @classmethod
    def choices(cls) -> tuple[str, ...]:
        return tuple(item.value for item in cls)


class TargetRole(DomainValue):
    UNSPECIFIED = "unspecified"
    PHYSICAL = "physical"
    COMPOSITE = "composite"


class TargetState(DomainValue):
    ACTIVE = "active"
    SYSTEM_ONLY = "system_only"
    REVIEW_ONLY = "review_only"
    SUPPRESSED = "suppressed"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


INACTIVE_TARGET_STATES = frozenset({
    TargetState.SUPPRESSED,
    TargetState.SUPERSEDED,
    TargetState.ARCHIVED,
})


class ProviderRunStatus(DomainValue):
    RUNNING = "running"
    MATCH = "match"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"


PROVIDER_FAILURE_STATUSES = frozenset({
    ProviderRunStatus.TRANSIENT_FAILURE,
    ProviderRunStatus.PERMANENT_FAILURE,
})

PROVIDER_QUERY_RESULT_STATUSES = frozenset({
    ProviderRunStatus.MATCH,
    ProviderRunStatus.NO_MATCH,
    ProviderRunStatus.AMBIGUOUS,
})

PROVIDER_REVIEW_STATUSES = frozenset({
    ProviderRunStatus.AMBIGUOUS,
    *PROVIDER_FAILURE_STATUSES,
})


class MeasurementTargetRole(DomainValue):
    CONTRIBUTOR = "contributor"
    COMPOSITE_SCOPE = "composite_scope"


class MeasurementAssociationActionKind(DomainValue):
    ASSIGN = "assign"
    UNASSIGN = "unassign"


class ReviewPriority(DomainValue):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    HIGHEST = "highest"

    @property
    def rank(self) -> int:
        return (
            ReviewPriority.NONE,
            ReviewPriority.LOW,
            ReviewPriority.MEDIUM,
            ReviewPriority.HIGH,
            ReviewPriority.HIGHEST,
        ).index(self)


def review_priority_rank(value: str | ReviewPriority) -> int:
    """Return increasing urgency from none (0) to highest (4)."""

    return ReviewPriority.parse(value, "review priority").rank
