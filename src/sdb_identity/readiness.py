from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .catalogs.results import effective_catalog_results
from .dirty import pending_export_targets
from .models.curated import CuratedRecord, DatasetRevision
from .models.identity import ExternalIdentifier
from .models.catalogs import IrasDetectionFamily
from .models.metadata import MetadataRun
from .models.samples import Sample, SampleExportRun
from .samples import SampleService
from .identifiers import normalize_identifier
from .photometry.state import load_system_photometry_state
from .update import DEFAULT_PROVIDERS
from .vocabulary import PROVIDER_REVIEW_STATUSES


DEFAULT_READINESS_PROVIDERS = (
    "simbad", "gaia_dr3", "tycho2", "2mass", "allwise",
)
@dataclass(frozen=True)
class ReadinessSummary:
    sample: str
    status: str
    target_count: int
    blocker_count: int
    warning_count: int
    pending_export_count: int
    sample_unresolved_curated_count: int
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
        members_by_id = {target.id: target for target in members}
        issues = []
        with self.sessions() as session:
            sample = session.scalar(select(Sample).where(Sample.name == sample_name))
            if sample is None:
                raise KeyError(f"sample not found: {sample_name}")
            photometry_state = load_system_photometry_state(
                session, members_by_id, expand_context=False,
            )
            catalog_providers = tuple(
                provider for provider in providers if provider != "simbad"
            )
            effective_results = effective_catalog_results(
                session, members_by_id, providers=catalog_providers,
            )
            measurements_by_target: dict[int, dict[int, object]] = defaultdict(dict)
            for encounter in photometry_state.encounters:
                measurements_by_target[encounter.target_id][
                    encounter.measurement.id
                ] = encounter.measurement
            for target in members:
                for provider in providers:
                    run = (
                        self._current_metadata_run(session, target.id, provider)
                        if provider == "simbad"
                        else effective_results.get((target.id, provider))
                    )
                    if run is None:
                        issues.append(self._issue(
                            "blocker", "missing_provider", target, provider,
                            "no current provider result",
                        ))
                    elif run.status in PROVIDER_REVIEW_STATUSES:
                        issues.append(self._issue(
                            "blocker", "provider_result", target, provider,
                            run.status, error=run.error,
                        ))
                measurements = list(
                    measurements_by_target.get(target.id, {}).values()
                )
                eligibility = photometry_state.eligibility
                excluded = sum(
                    eligibility[value.id].excluded
                    for value in measurements
                )
                shared = sum(value.ownership_scope == "shared" for value in measurements)
                blended = sum(value.blend_state != "clear" for value in measurements)
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
            sample_unresolved_curated, unresolved_curated = (
                self._curated_readiness(
                    session, members_by_id, issues,
                )
            )

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
            sample_unresolved_curated_count=sample_unresolved_curated,
            global_unresolved_curated_count=unresolved_curated,
            expected_providers=providers,
            issues=tuple(issues),
        )

    @staticmethod
    def _curated_readiness(session, members_by_id, issues):
        """Report unresolved curated rows only when aliases connect them here.

        Curated source files are intentionally database-wide. Rows with no
        connection to a selected sample remain useful global diagnostics but
        cannot block that sample's export.
        """
        identifiers: dict[str, set[int]] = defaultdict(set)
        if members_by_id:
            for target_id, normalized_value in session.execute(
                select(
                    ExternalIdentifier.target_id,
                    ExternalIdentifier.normalized_value,
                ).where(ExternalIdentifier.target_id.in_(members_by_id))
            ):
                identifiers[normalized_value].add(target_id)
        unresolved = list(session.execute(
            select(CuratedRecord, DatasetRevision)
            .join(
                DatasetRevision,
                DatasetRevision.id == CuratedRecord.revision_id,
            )
            .where(
                DatasetRevision.is_current.is_(True),
                CuratedRecord.association_status != "matched",
            )
            .order_by(DatasetRevision.dataset, CuratedRecord.record_no)
        ))
        sample_rows = []
        for record, revision in unresolved:
            if record.association_method == "manual_unassociated":
                continue
            candidate_ids = set()
            if record.target_id in members_by_id:
                candidate_ids.add(record.target_id)
            candidate_ids.update(
                identifiers.get(
                    normalize_identifier(record.source_identifier), set(),
                )
            )
            if not candidate_ids:
                continue
            candidates = [
                members_by_id[target_id]
                for target_id in sorted(candidate_ids)
                if target_id in members_by_id
            ]
            if not candidates:
                continue
            sample_rows.append(record)
            issue = {
                "severity": "blocker",
                "kind": "curated_record",
                "dataset": revision.dataset,
                "revision_id": revision.id,
                "record_no": record.record_no,
                "source_identifier": record.source_identifier,
                "association_status": record.association_status,
                "candidate_target_ids": [
                    target.id for target in candidates
                ],
                "candidate_sdbids": [
                    target.sdbid for target in candidates
                ],
                "detail": (
                    "unresolved curated record may belong to a sample target"
                ),
            }
            if len(candidates) == 1:
                issue["target_id"] = candidates[0].id
                issue["sdbid"] = candidates[0].sdbid
            issues.append(issue)
        return len(sample_rows), len(unresolved)

    @staticmethod
    def _current_metadata_run(session, target_id, provider):
        return session.scalar(select(MetadataRun).where(
            MetadataRun.target_id == target_id,
            MetadataRun.provider == provider,
            MetadataRun.is_current.is_(True),
        ).order_by(MetadataRun.id.desc()).limit(1))

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
