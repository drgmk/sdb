from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .catalogs.acquisition import CatalogAcquisitionService
from .metadata import MetadataService
from .models.catalogs import CatalogRun
from .models.metadata import MetadataRun
from .models.identity import Target
from .progress import NULL_PROGRESS, ProgressReporter
from .reference.application import ReferenceApplicationService
from .catalogs.registry import (
    REMOTE_CATALOG_PROVIDERS,
    SNAPSHOT_CATALOG_PROVIDERS,
)
from .reference.store import ReferenceStore
from .targets import resolve_target
from .vocabulary import PROVIDER_FAILURE_STATUSES


REMOTE_CATALOGS = REMOTE_CATALOG_PROVIDERS
SNAPSHOT_CATALOGS = SNAPSHOT_CATALOG_PROVIDERS
DEFAULT_PROVIDERS = ("simbad", *REMOTE_CATALOGS, *SNAPSHOT_CATALOGS)


@dataclass(frozen=True)
class UpdateItem:
    target_id: int | None
    sdbid: str | None
    provider: str
    action: str
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class UpdateSummary:
    target_count: int
    refreshed: int
    skipped: int
    missing: int
    failed: int
    items: tuple[UpdateItem, ...]


class UpdateService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        reference_store: ReferenceStore,
        *,
        metadata_factory: Callable[[], MetadataService],
        catalog_factory: Callable[[], CatalogAcquisitionService],
        workers: int = 4,
        bulk_chunk_size: int = 500,
        reporter: ProgressReporter | None = None,
    ):
        self.sessions = session_factory
        self.reference_store = reference_store
        self.metadata_factory = metadata_factory
        self.catalog_factory = catalog_factory
        self.workers = workers
        self.bulk_chunk_size = bulk_chunk_size
        self.reporter = reporter or NULL_PROGRESS

    def update_target(
        self,
        target_reference: str | int,
        *,
        force: bool = False,
        providers: Iterable[str] | None = None,
    ) -> UpdateSummary:
        selected = self._providers(providers)
        selected = tuple(
            provider
            for group in (
                ("simbad",) if "simbad" in selected else (),
                tuple(
                    provider for provider in selected
                    if provider in SNAPSHOT_CATALOGS
                ),
                tuple(
                    provider for provider in selected
                    if provider != "simbad" and provider not in SNAPSHOT_CATALOGS
                ),
            )
            for provider in group
        )
        with self.sessions() as session:
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            target_id, sdbid = target.id, target.sdbid
        items = []
        for provider in self.reporter.iter(
            selected,
            desc=f"Updating {sdbid}",
            total=len(selected),
            unit="provider",
        ):
            items.append(self._update_one(target_id, sdbid, provider, force=force))
        return self._summary(1, items)

    def update_all(
        self,
        *,
        force: bool = False,
        providers: Iterable[str] | None = None,
    ) -> UpdateSummary:
        selected = self._providers(providers)
        with self.sessions() as session:
            targets = [(target.id, target.sdbid) for target in session.scalars(
                select(Target).order_by(Target.id)
            )]
        current = self._current_remote_provider_targets(targets, selected)
        items: list[UpdateItem] = []

        if "simbad" in selected:
            if self._metadata_supports_many():
                items.extend(self._update_metadata_many(
                    targets,
                    "simbad",
                    force=force,
                    current_target_ids=current.get("simbad", frozenset()),
                ))
            else:
                items.extend(self._run_ordinary_updates(
                    targets, ["simbad"], force=force, current=current,
                ))

        snapshot_providers = [
            provider for provider in selected if provider in SNAPSHOT_CATALOGS
        ]
        for provider in self.reporter.iter(
            snapshot_providers,
            desc="Applying reference snapshots",
            total=len(snapshot_providers),
            unit="provider",
        ):
            snapshot = self.reference_store.current_snapshot(provider)
            if snapshot is None:
                items.append(UpdateItem(
                    None, None, provider, "missing", "missing",
                    f"run 'sdb reference fetch {provider}'",
                ))
                continue
            try:
                result = ReferenceApplicationService(
                    self.sessions, self.reference_store
                ).apply(provider, force=force)
                items.append(UpdateItem(
                    None,
                    None,
                    provider,
                    "skipped" if result.unchanged else "refreshed",
                    "unchanged" if result.unchanged else "completed",
                    f"targets={result.targets}; refreshed={result.refreshed}",
                ))
            except Exception as error:
                items.append(UpdateItem(
                    None, None, provider, "failed", "failed", str(error)
                ))

        remote = [provider for provider in selected if provider in REMOTE_CATALOGS]
        bulk_providers = []
        ordinary_providers = []
        for provider in remote:
            if hasattr(
                self.catalog_factory().adapters.get(provider), "query_many"
            ):
                bulk_providers.append(provider)
            else:
                ordinary_providers.append(provider)
        for provider in self.reporter.iter(
            bulk_providers,
            desc="Bulk provider refresh",
            total=len(bulk_providers),
            unit="provider",
        ):
            items.extend(self._update_catalog_many(
                targets, provider, force=force,
                current_target_ids=current.get(provider, frozenset()),
            ))
        items.extend(self._run_ordinary_updates(
            targets, ordinary_providers, force=force, current=current,
        ))
        return self._summary(len(targets), items)

    def update_targets(
        self,
        target_references: Iterable[str | int],
        *,
        force: bool = False,
        providers: Iterable[str] | None = None,
    ) -> UpdateSummary:
        """Update an explicit target subset without widening snapshot work."""
        selected = self._providers(providers)
        targets = []
        with self.sessions() as session:
            for reference in target_references:
                target = resolve_target(session, reference)
                if target is None:
                    raise KeyError(f"target not found: {reference}")
                targets.append((target.id, target.sdbid))
        targets = list(dict.fromkeys(targets))
        current = self._current_remote_provider_targets(targets, selected)
        items = []
        if "simbad" in selected:
            if self._metadata_supports_many():
                items.extend(self._update_metadata_many(
                    targets,
                    "simbad",
                    force=force,
                    current_target_ids=current.get("simbad", frozenset()),
                ))
            else:
                items.extend(self._run_ordinary_updates(
                    targets, ["simbad"], force=force, current=current,
                ))

        snapshot_providers = [
            provider for provider in selected if provider in SNAPSHOT_CATALOGS
        ]
        items.extend(self._run_ordinary_updates(
            targets, snapshot_providers, force=force, current=current,
        ))

        bulk_providers = []
        ordinary_providers = []
        for provider in (
            value for value in selected if value in REMOTE_CATALOGS
        ):
            if hasattr(
                self.catalog_factory().adapters.get(provider), "query_many"
            ):
                bulk_providers.append(provider)
            else:
                ordinary_providers.append(provider)
        for provider in self.reporter.iter(
            bulk_providers,
            desc="Bulk provider refresh",
            total=len(bulk_providers),
            unit="provider",
        ):
            items.extend(self._update_catalog_many(
                targets, provider, force=force,
                current_target_ids=current.get(provider, frozenset()),
            ))
        items.extend(self._run_ordinary_updates(
            targets, ordinary_providers, force=force, current=current,
        ))
        return self._summary(len(targets), items)

    def _update_catalog_many(
        self, targets, provider, *, force, current_target_ids=frozenset(),
    ):
        items = []
        pending = []
        for target_id, sdbid in targets:
            if not force and target_id in current_target_ids:
                items.append(UpdateItem(
                    target_id, sdbid, provider, "skipped", "current"
                ))
            else:
                pending.append((target_id, sdbid))
        if not pending:
            return items
        self.reporter.step(f"{provider}: bulk refresh for {len(pending)} targets")
        service = self.catalog_factory()
        try:
            refreshed = service.refresh_many(
                [target_id for target_id, _sdbid in pending],
                provider,
                chunk_size=self.bulk_chunk_size,
            )
        except Exception as error:
            items.extend(UpdateItem(
                target_id, sdbid, provider, "failed", "failed", str(error)
            ) for target_id, sdbid in pending)
            return items
        for result in refreshed:
            action = (
                "failed"
                if result.status in PROVIDER_FAILURE_STATUSES
                else "refreshed"
            )
            items.append(UpdateItem(
                result.target_id,
                next(sdbid for target_id, sdbid in pending if target_id == result.target_id),
                provider,
                action,
                result.status,
                result.error,
            ))
        return items

    def _update_metadata_many(
        self, targets, provider, *, force, current_target_ids=frozenset(),
    ):
        items = []
        pending = []
        for target_id, sdbid in targets:
            if not force and target_id in current_target_ids:
                items.append(UpdateItem(
                    target_id, sdbid, provider, "skipped", "current"
                ))
            else:
                pending.append((target_id, sdbid))
        if not pending:
            return items
        self.reporter.step(f"{provider}: bulk metadata refresh for {len(pending)} targets")
        service = self.metadata_factory()
        try:
            refreshed = service.refresh_many([target_id for target_id, _sdbid in pending])
        except Exception as error:
            items.extend(UpdateItem(
                target_id, sdbid, provider, "failed", "failed", str(error)
            ) for target_id, sdbid in pending)
            return items
        sdbid_by_target = dict(pending)
        for result in refreshed:
            action = (
                "failed"
                if result.status in PROVIDER_FAILURE_STATUSES
                else "refreshed"
            )
            items.append(UpdateItem(
                result.target_id,
                sdbid_by_target.get(result.target_id),
                provider,
                action,
                result.status,
                result.error,
            ))
        return items

    def _ordinary_update_jobs(
        self,
        targets: list[tuple[int, str]],
        providers: list[str],
        *,
        force: bool,
        current: dict[str, frozenset[int]] | None = None,
    ) -> tuple[list[UpdateItem], list[tuple[int, str, str]]]:
        skipped: list[UpdateItem] = []
        jobs: list[tuple[int, str, str]] = []
        for provider in providers:
            for target_id, sdbid in targets:
                is_current = (
                    target_id in (current or {}).get(provider, frozenset())
                    if provider == "simbad" or provider in REMOTE_CATALOGS
                    else self._ordinary_provider_is_current(target_id, provider)
                )
                if not force and is_current:
                    skipped.append(UpdateItem(
                        target_id, sdbid, provider, "skipped", "current"
                    ))
                else:
                    jobs.append((target_id, sdbid, provider))
        return skipped, jobs

    def _run_ordinary_updates(
        self,
        targets: list[tuple[int, str]],
        providers: list[str],
        *,
        force: bool,
        current: dict[str, frozenset[int]] | None = None,
    ) -> list[UpdateItem]:
        skipped, jobs = self._ordinary_update_jobs(
            targets, providers, force=force, current=current,
        )
        items = list(skipped)
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            items.extend(self.reporter.iter(
                executor.map(lambda job: self._update_one(*job, force=force), jobs),
                desc="Refreshing providers",
                total=len(jobs),
                unit="run",
            ))
        return items

    def _current_remote_provider_targets(
        self,
        targets: list[tuple[int, str]],
        providers: Iterable[str],
    ) -> dict[str, frozenset[int]]:
        """Load current remote-provider state in bounded set queries."""
        selected = tuple(dict.fromkeys(providers))
        result: dict[str, set[int]] = {provider: set() for provider in selected}
        target_ids = [target_id for target_id, _sdbid in targets]
        metadata_providers = [provider for provider in selected if provider == "simbad"]
        catalog_providers = [provider for provider in selected if provider in REMOTE_CATALOGS]
        with self.sessions() as session:
            for offset in range(0, len(target_ids), 500):
                chunk = target_ids[offset:offset + 500]
                if metadata_providers:
                    for target_id, provider in session.execute(
                        select(MetadataRun.target_id, MetadataRun.provider).where(
                            MetadataRun.target_id.in_(chunk),
                            MetadataRun.provider.in_(metadata_providers),
                            MetadataRun.is_current.is_(True),
                        )
                    ):
                        result[provider].add(target_id)
                if catalog_providers:
                    for target_id, provider in session.execute(
                        select(CatalogRun.target_id, CatalogRun.provider).where(
                            CatalogRun.target_id.in_(chunk),
                            CatalogRun.provider.in_(catalog_providers),
                            CatalogRun.is_current.is_(True),
                        )
                    ):
                        result[provider].add(target_id)
        return {provider: frozenset(values) for provider, values in result.items()}

    def _ordinary_provider_is_current(self, target_id: int, provider: str) -> bool:
        if provider == "simbad":
            return self._has_metadata(target_id, provider)
        if provider in REMOTE_CATALOGS:
            return self._has_catalog(target_id, provider)
        return False

    def _metadata_supports_many(self) -> bool:
        service = self.metadata_factory()
        return service.provider is not None and hasattr(service.provider, "query_many")

    def _update_one(
        self, target_id: int, sdbid: str, provider: str, *, force: bool
    ) -> UpdateItem:
        try:
            if provider == "simbad":
                if not force and self._has_metadata(target_id, provider):
                    return UpdateItem(target_id, sdbid, provider, "skipped", "current")
                result = self.metadata_factory().refresh(target_id)
            elif provider in SNAPSHOT_CATALOGS:
                snapshot = self.reference_store.current_snapshot(provider)
                if snapshot is None:
                    return UpdateItem(
                        target_id, sdbid, provider, "missing", "missing",
                        f"run 'sdb reference fetch {provider}'",
                    )
                from .catalogs.adapters.reference import snapshot_adapter
                adapter = snapshot_adapter(provider, self.reference_store)
                if not force and self._has_catalog(
                    target_id, provider, release=adapter.release
                ):
                    return UpdateItem(target_id, sdbid, provider, "skipped", "current")
                result = CatalogAcquisitionService(
                    self.sessions, {provider: adapter}
                ).refresh(target_id, provider)
            else:
                if not force and self._has_catalog(target_id, provider):
                    return UpdateItem(target_id, sdbid, provider, "skipped", "current")
                result = self.catalog_factory().refresh(target_id, provider)
            action = "refreshed"
            if result.status in PROVIDER_FAILURE_STATUSES:
                action = "failed"
            return UpdateItem(
                target_id,
                sdbid,
                provider,
                action,
                result.status,
                getattr(result, "error", None),
            )
        except Exception as error:
            return UpdateItem(target_id, sdbid, provider, "failed", "failed", str(error))

    def _has_catalog(
        self, target_id: int, provider: str, *, release: str | None = None
    ) -> bool:
        with self.sessions() as session:
            query = select(CatalogRun.id).where(
                CatalogRun.target_id == target_id,
                CatalogRun.provider == provider,
                CatalogRun.is_current.is_(True),
            )
            if release is not None:
                query = query.where(CatalogRun.release == release)
            return session.scalar(query.limit(1)) is not None

    def _has_metadata(self, target_id: int, provider: str) -> bool:
        with self.sessions() as session:
            return session.scalar(select(MetadataRun.id).where(
                MetadataRun.target_id == target_id,
                MetadataRun.provider == provider,
                MetadataRun.is_current.is_(True),
            ).limit(1)) is not None

    @staticmethod
    def _providers(providers: Iterable[str] | None) -> tuple[str, ...]:
        result = tuple(dict.fromkeys(providers or DEFAULT_PROVIDERS))
        unknown = set(result) - set(DEFAULT_PROVIDERS)
        if unknown:
            raise ValueError(f"unknown update providers: {', '.join(sorted(unknown))}")
        return result

    @staticmethod
    def _summary(target_count: int, items: list[UpdateItem]) -> UpdateSummary:
        return UpdateSummary(
            target_count=target_count,
            refreshed=sum(item.action == "refreshed" for item in items),
            skipped=sum(item.action == "skipped" for item in items),
            missing=sum(item.action == "missing" for item in items),
            failed=sum(item.action == "failed" for item in items),
            items=tuple(items),
        )
