from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from .dirty import pending_export_targets
from .catalog_measurements import current_measurements_for_target
from .models import (
    CatalogRun,
    IrasDetectionFamily,
    MetadataRun,
    PhotometryOverride,
    Sample,
    SampleExportRun,
)
from .samples import SampleService
from .update import DEFAULT_PROVIDERS


DEFAULT_READINESS_PROVIDERS = (
    "simbad", "gaia_dr3", "tycho2", "2mass", "allwise",
)
FAILURE_STATUSES = {"ambiguous", "transient_failure", "permanent_failure"}


@dataclass(frozen=True)
class ReadinessSummary:
    sample: str
    status: str
    target_count: int
    blocker_count: int
    warning_count: int
    pending_export_count: int
    global_unresolved_curated_count: int
    expected_providers: tuple[str, ...]
    issues: tuple[dict, ...]


class ReadinessService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.sessions = session_factory

    def report(
        self, sample_name: str, *, providers=DEFAULT_READINESS_PROVIDERS,
    ) -> ReadinessSummary:
        providers = tuple(dict.fromkeys(providers))
        if not providers:
            raise ValueError("at least one expected provider is required")
        unknown = set(providers) - set(DEFAULT_PROVIDERS)
        if unknown:
            raise ValueError(
                f"unknown readiness providers: {', '.join(sorted(unknown))}"
            )
        members = SampleService(self.sessions).members(sample_name)
        issues = []
        with self.sessions() as session:
            sample = session.scalar(select(Sample).where(Sample.name == sample_name))
            if sample is None:
                raise KeyError(f"sample not found: {sample_name}")
            for target in members:
                for provider in providers:
                    run = self._current_run(session, target.id, provider)
                    if run is None:
                        issues.append(self._issue(
                            "blocker", "missing_provider", target, provider,
                            "no current provider result",
                        ))
                    elif run.status in FAILURE_STATUSES:
                        issues.append(self._issue(
                            "blocker", "provider_result", target, provider,
                            run.status, error=run.error,
                        ))
                measurements = current_measurements_for_target(session, target.id)
                overrides = {
                    (value.provider, value.band): value
                    for value in session.scalars(
                        select(PhotometryOverride)
                        .where(PhotometryOverride.target_id == target.id)
                        .order_by(PhotometryOverride.id)
                    )
                }
                excluded = sum(
                    overrides.get((value.provider, value.band), value).excluded
                    for value in measurements
                )
                shared = sum(value.association_scope == "shared" for value in measurements)
                blended = sum(value.blend_status != "clear" for value in measurements)
                upper_limits = sum(value.upper_limit for value in measurements)
                private = sum(value.private for value in measurements)
                for kind, count, detail in (
                    ("excluded_photometry", excluded, "excluded measurements"),
                    ("shared_photometry", shared, "shared-source measurements"),
                    ("blended_photometry", blended, "provider-flagged blends"),
                    ("upper_limits", upper_limits, "upper limits"),
                    ("private_photometry", private, "private measurements"),
                ):
                    if count:
                        issues.append(self._issue(
                            "warning", kind, target, None, detail, count=count,
                        ))
                iras_review = session.scalar(select(func.count(IrasDetectionFamily.id)).where(
                    IrasDetectionFamily.target_id == target.id,
                    IrasDetectionFamily.is_current.is_(True),
                    IrasDetectionFamily.status == "review",
                )) or 0
                if iras_review:
                    issues.append(self._issue(
                        "blocker", "iras_family_review", target, "iras",
                        "unresolved PSC/FSC association", count=iras_review,
                    ))
            latest_export = session.scalar(
                select(SampleExportRun)
                .where(SampleExportRun.sample_id == sample.id)
                .order_by(SampleExportRun.id.desc())
                .limit(1)
            )
            if latest_export is not None and latest_export.status in {"partial", "running"}:
                issues.append({
                    "severity": "blocker",
                    "kind": "sample_export",
                    "run_id": latest_export.id,
                    "detail": f"latest sample export is {latest_export.status}",
                })
            unresolved_curated = session.execute(text(
                "SELECT COUNT(*) FROM unresolved_curated_records"
            )).scalar_one()

        pending = pending_export_targets(self.sessions, sample=sample_name)
        for target, event_count, dirty_since in pending:
            issues.append(self._issue(
                "warning", "pending_export", target, None,
                "target has unexported changes",
                count=event_count,
                dirty_since=dirty_since.isoformat(),
            ))
        blockers = sum(issue["severity"] == "blocker" for issue in issues)
        warnings = sum(issue["severity"] == "warning" for issue in issues)
        return ReadinessSummary(
            sample=sample_name,
            status="blocked" if blockers else ("review" if warnings else "ready"),
            target_count=len(members),
            blocker_count=blockers,
            warning_count=warnings,
            pending_export_count=len(pending),
            global_unresolved_curated_count=unresolved_curated,
            expected_providers=providers,
            issues=tuple(issues),
        )

    @staticmethod
    def _current_run(session, target_id, provider):
        model = MetadataRun if provider == "simbad" else CatalogRun
        return session.scalar(select(model).where(
            model.target_id == target_id,
            model.provider == provider,
            model.is_current.is_(True),
        ).order_by(model.id.desc()).limit(1))

    @staticmethod
    def _issue(severity, kind, target, provider, detail, **extra):
        value = {
            "severity": severity,
            "kind": kind,
            "target_id": target.id,
            "sdbid": target.sdbid,
            "detail": detail,
        }
        if provider is not None:
            value["provider"] = provider
        value.update(extra)
        return value
