from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .dirty import mark_export_dirty
from .models.identity import AstrometricSolution, ExternalIdentifier, Target
from .models.metadata import (
    MetadataRun,
    SimbadMetadata,
    SimbadObjectType,
    SimbadRelationship,
    UserNote,
)
from .providers import Astrometry, ProviderError
from .identifiers import normalize_identifier
from .targets import resolve_target
from .vocabulary import (
    PROVIDER_QUERY_RESULT_STATUSES,
    ProviderRunStatus,
)


@dataclass(frozen=True)
class ObjectTypeValue:
    object_type: str
    label: str | None
    description: str | None
    is_primary: bool = False


@dataclass(frozen=True)
class RelationshipValue:
    direction: str
    related_oid: int
    related_main_id: str
    related_ra_deg: float | None
    related_dec_deg: float | None
    membership_percent: int | None
    link_bibcode: str | None
    separation_arcsec: float | None
    related_object_type: str | None = None
    related_object_types: tuple[str, ...] = field(default_factory=tuple)
    related_spectral_type: str | None = None
    related_spectral_type_bibcode: str | None = None


@dataclass(frozen=True)
class SimbadSnapshot:
    oid: int
    main_id: str
    ra_deg: float
    dec_deg: float
    identifiers: tuple[str, ...] = field(default_factory=tuple)
    spectral_type: str | None = None
    spectral_type_bibcode: str | None = None
    parallax_mas: float | None = None
    parallax_error_mas: float | None = None
    parallax_bibcode: str | None = None
    pm_ra_cosdec_masyr: float | None = None
    pm_dec_masyr: float | None = None
    proper_motion_bibcode: str | None = None
    radial_velocity_kms: float | None = None
    radial_velocity_error_kms: float | None = None
    radial_velocity_bibcode: str | None = None
    primary_object_type: str | None = None
    object_types: tuple[ObjectTypeValue, ...] = field(default_factory=tuple)
    relationships: tuple[RelationshipValue, ...] = field(default_factory=tuple)
    raw: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MetadataQueryResult:
    status: str | ProviderRunStatus
    candidates: tuple[SimbadSnapshot, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MetadataQueryContext:
    target_id: int
    sdbid: str
    identifiers: tuple[str, ...]
    astrometry: Astrometry

    @property
    def preferred_identifier(self) -> str | None:
        return self.identifiers[0] if self.identifiers else None


class MetadataProvider(Protocol):
    name: str
    release: str

    def query(self, context: MetadataQueryContext) -> MetadataQueryResult: ...


@dataclass(frozen=True)
class MetadataRefreshResult:
    run_id: int
    target_id: int
    provider: str
    status: ProviderRunStatus
    candidate_count: int
    main_id: str | None = None
    error: str | None = None


class MetadataService:
    def __init__(self, session_factory: sessionmaker[Session], provider: MetadataProvider | None):
        self.sessions = session_factory
        self.provider = provider

    def refresh(self, target_reference: str | int) -> MetadataRefreshResult:
        if self.provider is None:
            raise RuntimeError("metadata refresh requires a provider")
        with self.sessions() as session:
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            context = self._context(session, target)
            previous = session.scalar(select(MetadataRun).where(
                MetadataRun.target_id == target.id,
                MetadataRun.provider == self.provider.name,
                MetadataRun.is_current.is_(True),
            ))
            previous_signature = None if previous is None else (
                previous.status,
                previous.raw_response_json,
            )
            session.execute(
                update(MetadataRun)
                .where(
                    MetadataRun.target_id == target.id,
                    MetadataRun.provider == self.provider.name,
                    MetadataRun.status == ProviderRunStatus.RUNNING,
                )
                .values(
                    status=ProviderRunStatus.TRANSIENT_FAILURE,
                    error="superseded after interrupted refresh",
                    completed_at=datetime.now(timezone.utc),
                    is_current=False,
                )
            )
            run = MetadataRun(
                target_id=target.id,
                provider=self.provider.name,
                release=self.provider.release,
                status=ProviderRunStatus.RUNNING,
                is_current=False,
                query_identifier=context.preferred_identifier,
            )
            session.add(run)
            session.flush()
            # Keep the potentially slow TAP request outside a SQLite write
            # transaction while retaining a durable running record.
            session.commit()
            try:
                result = self.provider.query(context)
            except ProviderError as error:
                run.status = (
                    ProviderRunStatus.TRANSIENT_FAILURE
                    if error.transient
                    else ProviderRunStatus.PERMANENT_FAILURE
                )
                run.error = str(error)
                run.completed_at = datetime.now(timezone.utc)
                session.commit()
                return MetadataRefreshResult(
                    run.id,
                    target.id,
                    self.provider.name,
                    ProviderRunStatus.parse(run.status, "metadata status"),
                    0,
                    error=str(error),
                )

            result_status = ProviderRunStatus.parse(result.status, "metadata status")
            if result_status not in PROVIDER_QUERY_RESULT_STATUSES:
                raise ValueError(f"invalid metadata result status: {result.status}")
            run.status = result_status
            run.candidate_count = len(result.candidates)
            run.raw_response_json = json.dumps(
                [candidate.raw for candidate in result.candidates],
                sort_keys=True,
                ensure_ascii=False,
            )
            main_id = None
            if result_status is ProviderRunStatus.MATCH:
                if len(result.candidates) != 1:
                    raise ValueError("metadata match must contain exactly one candidate")
                value = result.candidates[0]
                main_id = value.main_id
                session.add(
                    SimbadMetadata(
                        run_id=run.id,
                        target_id=target.id,
                        oid=value.oid,
                        main_id=value.main_id,
                        ra_deg=value.ra_deg,
                        dec_deg=value.dec_deg,
                        spectral_type=value.spectral_type,
                        spectral_type_bibcode=value.spectral_type_bibcode,
                        parallax_mas=value.parallax_mas,
                        parallax_error_mas=value.parallax_error_mas,
                        parallax_bibcode=value.parallax_bibcode,
                        pm_ra_cosdec_masyr=value.pm_ra_cosdec_masyr,
                        pm_dec_masyr=value.pm_dec_masyr,
                        proper_motion_bibcode=value.proper_motion_bibcode,
                        radial_velocity_kms=value.radial_velocity_kms,
                        radial_velocity_error_kms=value.radial_velocity_error_kms,
                        radial_velocity_bibcode=value.radial_velocity_bibcode,
                        primary_object_type=value.primary_object_type,
                    )
                )
                for object_type in value.object_types:
                    session.add(
                        SimbadObjectType(
                            run_id=run.id,
                            target_id=target.id,
                            object_type=object_type.object_type,
                            label=object_type.label,
                            description=object_type.description,
                            is_primary=object_type.is_primary,
                        )
                    )
                for relationship in value.relationships:
                    session.add(
                        SimbadRelationship(
                            run_id=run.id,
                            target_id=target.id,
                            direction=relationship.direction,
                            related_oid=relationship.related_oid,
                            related_main_id=relationship.related_main_id,
                            related_ra_deg=relationship.related_ra_deg,
                            related_dec_deg=relationship.related_dec_deg,
                            related_object_type=relationship.related_object_type,
                            related_object_types_json=json.dumps(relationship.related_object_types, sort_keys=True),
                            related_spectral_type=relationship.related_spectral_type,
                            related_spectral_type_bibcode=relationship.related_spectral_type_bibcode,
                            membership_percent=relationship.membership_percent,
                            link_bibcode=relationship.link_bibcode,
                            separation_arcsec=relationship.separation_arcsec,
                        )
                    )
                self._store_identifiers(
                    session,
                    target.id,
                    (value.main_id, *value.identifiers),
                )

            session.execute(
                update(MetadataRun)
                .where(
                    MetadataRun.target_id == target.id,
                    MetadataRun.provider == self.provider.name,
                    MetadataRun.is_current.is_(True),
                    MetadataRun.id != run.id,
                )
                .values(is_current=False)
            )
            run.is_current = True
            run.completed_at = datetime.now(timezone.utc)
            if previous_signature != (run.status, run.raw_response_json):
                mark_export_dirty(
                    session,
                    target.id,
                    source_type="metadata",
                    source_id=run.id,
                    reason=f"{self.provider.name} metadata result changed",
                )
            session.commit()
            return MetadataRefreshResult(
                run.id,
                target.id,
                self.provider.name,
                ProviderRunStatus.parse(run.status, "metadata status"),
                run.candidate_count,
                main_id,
            )

    def refresh_many(
        self,
        target_references: tuple[str | int, ...] | list[str | int],
    ) -> tuple[MetadataRefreshResult, ...]:
        if self.provider is None:
            raise RuntimeError("metadata refresh requires a provider")
        references = tuple(dict.fromkeys(target_references))
        if not hasattr(self.provider, "query_many"):
            return tuple(self.refresh(reference) for reference in references)
        with self.sessions() as session:
            contexts = []
            for reference in references:
                target = resolve_target(session, reference)
                if target is None:
                    raise KeyError(f"target not found: {reference}")
                contexts.append(self._context(session, target))
        if not contexts:
            return ()

        run_ids: dict[int, int] = {}
        previous_signatures: dict[int, tuple[str, str | None] | None] = {}
        with self.sessions() as session:
            for context in contexts:
                previous = session.scalar(select(MetadataRun).where(
                    MetadataRun.target_id == context.target_id,
                    MetadataRun.provider == self.provider.name,
                    MetadataRun.is_current.is_(True),
                ))
                previous_signatures[context.target_id] = None if previous is None else (
                    previous.status,
                    previous.raw_response_json,
                )
                session.execute(
                    update(MetadataRun)
                    .where(
                        MetadataRun.target_id == context.target_id,
                        MetadataRun.provider == self.provider.name,
                        MetadataRun.status == ProviderRunStatus.RUNNING,
                    )
                    .values(
                        status=ProviderRunStatus.TRANSIENT_FAILURE,
                        error="superseded after interrupted refresh",
                        completed_at=datetime.now(timezone.utc),
                        is_current=False,
                    )
                )
                run = MetadataRun(
                    target_id=context.target_id,
                    provider=self.provider.name,
                    release=self.provider.release,
                    status=ProviderRunStatus.RUNNING,
                    is_current=False,
                    query_identifier=context.preferred_identifier,
                )
                session.add(run)
                session.flush()
                run_ids[context.target_id] = run.id
            session.commit()

        try:
            results_by_target = self.provider.query_many(tuple(contexts))
        except ProviderError as error:
            status = (
                ProviderRunStatus.TRANSIENT_FAILURE
                if error.transient
                else ProviderRunStatus.PERMANENT_FAILURE
            )
            completed_at = datetime.now(timezone.utc)
            with self.sessions() as session:
                for context in contexts:
                    run = session.get(MetadataRun, run_ids[context.target_id])
                    run.status = status
                    run.error = str(error)
                    run.completed_at = completed_at
                session.commit()
            return tuple(
                MetadataRefreshResult(
                    run_ids[context.target_id],
                    context.target_id,
                    self.provider.name,
                    status,
                    0,
                    error=str(error),
                )
                for context in contexts
            )

        values = []
        with self.sessions() as session:
            for context in contexts:
                run = session.get(MetadataRun, run_ids[context.target_id])
                result = results_by_target.get(
                    context.target_id,
                    MetadataQueryResult(ProviderRunStatus.NO_MATCH),
                )
                values.append(self._store_result(
                    session,
                    run,
                    result,
                    previous_signatures[context.target_id],
                ))
            session.commit()
        return tuple(values)

    def _store_result(
        self,
        session: Session,
        run: MetadataRun,
        result: MetadataQueryResult,
        previous_signature: tuple[str, str | None] | None,
    ) -> MetadataRefreshResult:
        result_status = ProviderRunStatus.parse(result.status, "metadata status")
        if result_status not in PROVIDER_QUERY_RESULT_STATUSES:
            raise ValueError(f"invalid metadata result status: {result.status}")
        run.status = result_status
        run.candidate_count = len(result.candidates)
        run.raw_response_json = json.dumps(
            [candidate.raw for candidate in result.candidates],
            sort_keys=True,
            ensure_ascii=False,
        )
        main_id = None
        if result_status is ProviderRunStatus.MATCH:
            if len(result.candidates) != 1:
                raise ValueError("metadata match must contain exactly one candidate")
            value = result.candidates[0]
            main_id = value.main_id
            session.add(
                SimbadMetadata(
                    run_id=run.id,
                    target_id=run.target_id,
                    oid=value.oid,
                    main_id=value.main_id,
                    ra_deg=value.ra_deg,
                    dec_deg=value.dec_deg,
                    spectral_type=value.spectral_type,
                    spectral_type_bibcode=value.spectral_type_bibcode,
                    parallax_mas=value.parallax_mas,
                    parallax_error_mas=value.parallax_error_mas,
                    parallax_bibcode=value.parallax_bibcode,
                    pm_ra_cosdec_masyr=value.pm_ra_cosdec_masyr,
                    pm_dec_masyr=value.pm_dec_masyr,
                    proper_motion_bibcode=value.proper_motion_bibcode,
                    radial_velocity_kms=value.radial_velocity_kms,
                    radial_velocity_error_kms=value.radial_velocity_error_kms,
                    radial_velocity_bibcode=value.radial_velocity_bibcode,
                    primary_object_type=value.primary_object_type,
                )
            )
            for object_type in value.object_types:
                session.add(
                    SimbadObjectType(
                        run_id=run.id,
                        target_id=run.target_id,
                        object_type=object_type.object_type,
                        label=object_type.label,
                        description=object_type.description,
                        is_primary=object_type.is_primary,
                    )
                )
            for relationship in value.relationships:
                session.add(
                    SimbadRelationship(
                        run_id=run.id,
                        target_id=run.target_id,
                        direction=relationship.direction,
                        related_oid=relationship.related_oid,
                        related_main_id=relationship.related_main_id,
                        related_ra_deg=relationship.related_ra_deg,
                        related_dec_deg=relationship.related_dec_deg,
                        related_object_type=relationship.related_object_type,
                        related_object_types_json=json.dumps(relationship.related_object_types, sort_keys=True),
                        related_spectral_type=relationship.related_spectral_type,
                        related_spectral_type_bibcode=relationship.related_spectral_type_bibcode,
                        membership_percent=relationship.membership_percent,
                        link_bibcode=relationship.link_bibcode,
                        separation_arcsec=relationship.separation_arcsec,
                    )
                )
            self._store_identifiers(
                session,
                run.target_id,
                (value.main_id, *value.identifiers),
            )

        session.execute(
            update(MetadataRun)
            .where(
                MetadataRun.target_id == run.target_id,
                MetadataRun.provider == self.provider.name,
                MetadataRun.is_current.is_(True),
                MetadataRun.id != run.id,
            )
            .values(is_current=False)
        )
        run.is_current = True
        run.completed_at = datetime.now(timezone.utc)
        if previous_signature != (run.status, run.raw_response_json):
            mark_export_dirty(
                session,
                run.target_id,
                source_type="metadata",
                source_id=run.id,
                reason=f"{self.provider.name} metadata result changed",
            )
        return MetadataRefreshResult(
            run.id,
            run.target_id,
            self.provider.name,
            ProviderRunStatus.parse(run.status, "metadata status"),
            run.candidate_count,
            main_id,
        )

    def add_note(self, target_reference: str | int, text: str, *, actor: str) -> UserNote:
        if not text.strip():
            raise ValueError("note text cannot be empty")
        if not actor.strip():
            raise ValueError("note actor cannot be empty")
        with self.sessions.begin() as session:
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            note = UserNote(target_id=target.id, actor=actor.strip(), text=text.strip())
            session.add(note)
            session.flush()
        return note

    def list_notes(self, target_reference: str | int) -> list[UserNote]:
        with self.sessions() as session:
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            return list(
                session.scalars(
                    select(UserNote)
                    .where(UserNote.target_id == target.id)
                    .order_by(UserNote.id)
                )
            )

    @staticmethod
    def _context(session: Session, target: Target) -> MetadataQueryContext:
        identifiers = list(
            session.scalars(
                select(ExternalIdentifier)
                .where(ExternalIdentifier.target_id == target.id)
                .order_by(ExternalIdentifier.id)
            )
        )
        priority = {"simbad_metadata": 0, "simbad": 0, "submitted": 1, "2mass": 2, "gaia_dr3": 3, "sdb": 9}
        identifiers.sort(key=lambda value: (priority.get(value.source, 5), value.id))
        names = tuple(value.value for value in identifiers if value.source != "sdb")
        solution = session.get(AstrometricSolution, target.canonical_astrometry_id)
        astrometry = Astrometry(
            target.ra2000_deg,
            target.dec2000_deg,
            2000.0,
            source="sdb",
            source_id=target.sdbid,
        )
        if solution is not None:
            astrometry = Astrometry(
                solution.ra_deg,
                solution.dec_deg,
                solution.epoch,
                solution.pm_ra_cosdec_masyr,
                solution.pm_dec_masyr,
                solution.parallax_mas,
                solution.radial_velocity_kms,
                solution.source,
                solution.source_id,
            )
        return MetadataQueryContext(target.id, target.sdbid, names, astrometry)

    @staticmethod
    def _store_identifiers(
        session: Session,
        target_id: int,
        identifiers: tuple[str, ...],
    ) -> None:
        seen: set[str] = set()
        for identifier in identifiers:
            if not identifier:
                continue
            normalized = normalize_identifier(identifier)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            existing = session.scalar(
                select(ExternalIdentifier).where(
                    ExternalIdentifier.target_id == target_id,
                    ExternalIdentifier.normalized_value == normalized,
                )
            )
            if existing is None:
                session.add(
                    ExternalIdentifier(
                        target_id=target_id,
                        value=identifier,
                        normalized_value=normalized,
                        source="simbad_metadata",
                    )
                )
