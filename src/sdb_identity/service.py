from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import angular_separation_arcsec, make_sdbid, propagate_to_epoch, validate_position
from .models.identity import (
    AstrometricSolution,
    ExternalIdentifier,
    MatchCandidate,
    MatchDecision,
    ProviderOutcome,
    Submission,
    Target,
)
from .models.metadata import MetadataRun, SimbadMetadata
from .decisions import DecisionContext
from .dirty import mark_export_dirty
from .identifiers import normalize_identifier
from .identity_results import effective_identity_candidate_ids
from .providers import Astrometry, Candidate, GaiaProvider, NullGaia, NullSimbad, ProviderError, SimbadProvider
from .vocabulary import ProviderRunStatus

_GAIA_DR3_IDENTIFIER = re.compile(r"^Gaia\s+DR3\s+(\d+)$", re.IGNORECASE)
_COMPONENT_IDENTITY_RE = re.compile(
    r"(?:\s+|(?<=\d))([A-Z]{1,3}|[A-Z][a-z0-9])$"
)
_UNSET_RESOLUTION = object()


class UnresolvedTarget(ValueError):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class AddRequest:
    name: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    epoch: float = 2000.0
    command: str | None = None
    pm_ra_cosdec_masyr: float | None = None
    pm_dec_masyr: float | None = None

    def validate(self) -> None:
        if not self.name and (self.ra_deg is None or self.dec_deg is None):
            raise ValueError("provide a name or both --ra and --dec")
        if (self.ra_deg is None) != (self.dec_deg is None):
            raise ValueError("--ra and --dec must be provided together")
        if (self.pm_ra_cosdec_masyr is None) != (self.pm_dec_masyr is None):
            raise ValueError("proper motion requires both RA and Dec components")
        if self.pm_ra_cosdec_masyr is not None and self.ra_deg is None:
            raise ValueError("proper motion requires an input position")
        if self.pm_ra_cosdec_masyr is not None and not all(math.isfinite(value) for value in (
            self.pm_ra_cosdec_masyr, self.pm_dec_masyr,
        )):
            raise ValueError("proper-motion components must be finite")
        if self.ra_deg is not None:
            validate_position(self.ra_deg, self.dec_deg)


@dataclass(frozen=True)
class AddResult:
    target_id: int
    sdbid: str
    created: bool
    astrometry_source: str


@dataclass(frozen=True)
class IdentityProviderOutcome:
    provider: str
    status: ProviderRunStatus
    message: str | None = None


@dataclass(frozen=True)
class ScoredIdentityCandidate:
    candidate: Candidate
    separation_arcsec: float
    score: float
    identifier_agreement: bool
    accepted: bool


@dataclass(frozen=True)
class IdentityResolution:
    selected_astrometry: Astrometry | None
    identity_main_id: str | None
    identifiers: tuple[tuple[str, str], ...]
    candidates: tuple[ScoredIdentityCandidate, ...]
    outcomes: tuple[IdentityProviderOutcome, ...]
    error: str | None = None
    transient_failure: bool = False


class IdentityResolver:
    """Resolve remote identity evidence without writing database state."""

    def __init__(
        self,
        *,
        simbad: SimbadProvider | None = None,
        gaia: GaiaProvider | None = None,
        acceptance_score: float = 0.5,
        acceptance_margin: float = 0.15,
    ):
        self.simbad = simbad or NullSimbad()
        self.gaia = gaia or NullGaia()
        self.acceptance_score = acceptance_score
        self.acceptance_margin = acceptance_margin

    def resolve(
        self,
        request: AddRequest,
        *,
        name_resolution: object = _UNSET_RESOLUTION,
    ) -> IdentityResolution:
        request.validate()
        base = None
        identity_main_id = None
        transient_failure = False
        resolution_error = None
        identifiers: list[tuple[str, str]] = []
        outcomes: list[IdentityProviderOutcome] = []
        if request.ra_deg is not None:
            base = Astrometry(
                request.ra_deg,
                request.dec_deg,
                request.epoch,
                pm_ra_cosdec_masyr=request.pm_ra_cosdec_masyr,
                pm_dec_masyr=request.pm_dec_masyr,
                source="input",
            )

        if request.name:
            if name_resolution is not _UNSET_RESOLUTION:
                resolution = name_resolution
                outcomes.append(IdentityProviderOutcome(
                    self.simbad.name,
                    (
                        ProviderRunStatus.MATCH
                        if resolution
                        else ProviderRunStatus.NO_MATCH
                    ),
                ))
            else:
                try:
                    resolution = self.simbad.resolve_name(request.name)
                except ProviderError as error:
                    outcomes.append(IdentityProviderOutcome(
                        self.simbad.name,
                        (
                            ProviderRunStatus.TRANSIENT_FAILURE
                            if error.transient
                            else ProviderRunStatus.PERMANENT_FAILURE
                        ),
                        str(error),
                    ))
                    transient_failure = error.transient
                    resolution_error = str(error)
                    resolution = None
                else:
                    outcomes.append(IdentityProviderOutcome(
                        self.simbad.name,
                        (
                            ProviderRunStatus.MATCH
                            if resolution
                            else ProviderRunStatus.NO_MATCH
                        ),
                    ))
            if resolution:
                identity_main_id = resolution.main_id
                resolved = resolution.astrometry.with_source(
                    "simbad", resolution.main_id,
                )
                if base is None:
                    base = resolved
                elif (
                    not base.proper_motion_available
                    and resolved.proper_motion_available
                ):
                    resolved_at_input = propagate_to_epoch(
                        resolved, base.epoch,
                    )
                    if angular_separation_arcsec(
                        base, resolved_at_input,
                    ) <= 10.0:
                        base = resolved
                identifiers.extend(
                    (identifier, "simbad")
                    for identifier in (
                        resolution.main_id,
                        *resolution.identifiers,
                    )
                )
            identifiers.append((request.name, "submitted"))

        if base is None:
            message = "name could not be resolved to coordinates"
            if resolution_error:
                message += f"; {resolution_error}"
            return IdentityResolution(
                None,
                identity_main_id,
                tuple(identifiers),
                (),
                tuple(outcomes),
                error=message,
                transient_failure=transient_failure,
            )

        selected = base
        scored: list[tuple[Candidate, float, float, bool]] = []
        try:
            gaia_candidates = self.gaia.search(base)
        except ProviderError as error:
            outcomes.append(IdentityProviderOutcome(
                self.gaia.name,
                (
                    ProviderRunStatus.TRANSIENT_FAILURE
                    if error.transient
                    else ProviderRunStatus.PERMANENT_FAILURE
                ),
                str(error),
            ))
            gaia_candidates = []
        else:
            outcomes.append(IdentityProviderOutcome(
                self.gaia.name,
                (
                    ProviderRunStatus.MATCH
                    if gaia_candidates
                    else ProviderRunStatus.NO_MATCH
                ),
            ))

        simbad_gaia_ids = gaia_dr3_identifiers_from_pairs(identifiers)
        normalized_identifiers = {
            normalize_identifier(value)
            for value in identifiers_from_pairs(identifiers)
        }
        for candidate in gaia_candidates:
            separation = angular_separation_arcsec(
                base,
                candidate.astrometry,
                epoch=candidate.astrometry.epoch,
            )
            if separation > 10.0:
                continue
            common = normalized_identifiers & {
                normalize_identifier(value)
                for value in candidate.identifiers
            }
            identifier_agreement = candidate.source_id in simbad_gaia_ids
            positional_score = math.exp(
                -0.5 * (separation / 2.0) ** 2
            )
            if simbad_gaia_ids:
                score = (
                    positional_score + 0.75
                    if identifier_agreement
                    else positional_score * 0.25
                )
            else:
                score = positional_score + (0.25 if common else 0.0)
            scored.append((
                candidate,
                separation,
                min(score, 1.0),
                identifier_agreement,
            ))
        scored.sort(key=lambda item: item[2], reverse=True)
        accepted_index = None
        if scored:
            best_score = scored[0][2]
            runner_up = scored[1][2] if len(scored) > 1 else 0.0
            if (
                best_score >= self.acceptance_score
                and best_score - runner_up >= self.acceptance_margin
            ):
                accepted_index = 0
                accepted = scored[0][0]
                accepted_astrometry = accepted.astrometry.with_source(
                    "gaia_dr3", accepted.source_id,
                )
                if _should_replace_astrometry(selected, accepted_astrometry):
                    selected = accepted_astrometry
                identifiers.append((
                    f"Gaia DR3 {accepted.source_id}", "gaia_dr3",
                ))
                identifiers.extend(
                    (value, "gaia_dr3")
                    for value in accepted.identifiers
                )

        return IdentityResolution(
            selected,
            identity_main_id,
            tuple(identifiers),
            tuple(
                ScoredIdentityCandidate(
                    candidate,
                    separation,
                    score,
                    identifier_agreement,
                    index == accepted_index,
                )
                for index, (
                    candidate,
                    separation,
                    score,
                    identifier_agreement,
                ) in enumerate(scored)
            ),
            tuple(outcomes),
        )


def _component_identity_qualifier(value: str | None) -> str | None:
    if not value:
        return None
    matched = _COMPONENT_IDENTITY_RE.search(" ".join(value.strip().split()))
    if matched is None:
        return None
    return matched.group(1).upper()


def _should_replace_astrometry(current: Astrometry, candidate: Astrometry) -> bool:
    if candidate.source == "gaia_dr3":
        if current.proper_motion_available and not candidate.proper_motion_available:
            return False
    return True


def _same_astrometry_solution(first: Astrometry, second: Astrometry) -> bool:
    return (
        first.source == second.source
        and first.source_id == second.source_id
        and first.epoch == second.epoch
        and first.ra_deg == second.ra_deg
        and first.dec_deg == second.dec_deg
    )


def _astrometric_solution(
    target_id: int,
    value: Astrometry,
    *,
    derived_ra2000_deg: float | None = None,
    derived_dec2000_deg: float | None = None,
) -> AstrometricSolution:
    derived = (
        None
        if derived_ra2000_deg is not None and derived_dec2000_deg is not None
        else propagate_to_epoch(value, 2000.0)
    )
    return AstrometricSolution(
        target_id=target_id,
        source=value.source,
        source_id=value.source_id,
        ra_deg=value.ra_deg,
        dec_deg=value.dec_deg,
        epoch=value.epoch,
        pm_ra_cosdec_masyr=value.pm_ra_cosdec_masyr,
        pm_dec_masyr=value.pm_dec_masyr,
        proper_motion_available=value.proper_motion_available,
        parallax_mas=value.parallax_mas,
        radial_velocity_kms=value.radial_velocity_kms,
        position_bibcode=value.position_bibcode,
        proper_motion_bibcode=value.proper_motion_bibcode,
        parallax_bibcode=value.parallax_bibcode,
        radial_velocity_bibcode=value.radial_velocity_bibcode,
        derived_ra2000_deg=derived_ra2000_deg if derived is None else derived.ra_deg,
        derived_dec2000_deg=derived_dec2000_deg if derived is None else derived.dec_deg,
    )


@dataclass(frozen=True)
class TargetRegistration:
    target: Target
    created: bool
    derived_astrometry: Astrometry
    component_qualifier: str | None


class TargetRegistrar:
    """Deduplicate and persist a resolved target and canonical astrometry."""

    def __init__(self, *, duplicate_radius_arcsec: float = 0.36):
        self.duplicate_radius_arcsec = duplicate_radius_arcsec

    def register_target(
        self,
        session: Session,
        submission: Submission,
        resolution: IdentityResolution,
    ) -> TargetRegistration:
        selected = resolution.selected_astrometry
        if selected is None:
            raise ValueError("cannot register an unresolved identity")
        derived = propagate_to_epoch(selected, 2000.0)
        base_sdbid = make_sdbid(derived.ra_deg, derived.dec_deg)
        component_qualifier = _component_identity_qualifier(
            resolution.identity_main_id
        )
        target = self.find_existing(
            session,
            base_sdbid,
            derived,
            resolution.identifiers,
            identity_main_id=resolution.identity_main_id,
            component_qualifier=component_qualifier,
        )
        created = target is None
        if target is None:
            sdbid = self.available_sdbid(
                session,
                base_sdbid,
                component_qualifier=component_qualifier,
            )
            target = Target(
                sdbid=sdbid,
                ra2000_deg=derived.ra_deg,
                dec2000_deg=derived.dec_deg,
            )
            session.add(target)
            session.flush()
            mark_export_dirty(
                session,
                target.id,
                source_type="identity",
                source_id=submission.id,
                reason="target created",
            )
            solution = _astrometric_solution(
                target.id,
                selected,
                derived_ra2000_deg=derived.ra_deg,
                derived_dec2000_deg=derived.dec_deg,
            )
            session.add(solution)
            session.flush()
            target.canonical_astrometry_id = solution.id
            accepted = next(
                (value for value in resolution.candidates if value.accepted),
                None,
            )
            if accepted is not None:
                accepted_astrometry = (
                    accepted.candidate.astrometry.with_source(
                        "gaia_dr3", accepted.candidate.source_id,
                    )
                )
                if not _same_astrometry_solution(
                    selected, accepted_astrometry,
                ):
                    session.add(_astrometric_solution(
                        target.id, accepted_astrometry,
                    ))
        return TargetRegistration(
            target,
            created,
            derived,
            component_qualifier,
        )

    def find_existing(
        self,
        session: Session,
        sdbid: str,
        derived: Astrometry,
        identifiers: Sequence[tuple[str, str]],
        *,
        identity_main_id: str | None,
        component_qualifier: str | None,
    ) -> Target | None:
        norms = [
            normalize_identifier(value)
            for value in identifiers_from_pairs(identifiers)
        ]
        matching_target_ids: set[int] = set()
        if norms:
            matching_target_ids = set(session.scalars(
                select(ExternalIdentifier.target_id).where(
                    ExternalIdentifier.normalized_value.in_(norms)
                )
            ))
            identity_norm = normalize_identifier(identity_main_id or "")
            for target_id in sorted(matching_target_ids):
                target = session.get(Target, target_id)
                if target is not None and normalize_identifier(
                    self.target_primary_identity(session, target) or ""
                ) == identity_norm:
                    return target
        target = session.scalar(
            select(Target).where(Target.sdbid == sdbid)
        )
        if target and not self.component_identity_conflicts(
            session, target, identity_main_id, component_qualifier,
        ):
            return target
        for target_id in sorted(matching_target_ids):
            candidate = session.get(Target, target_id)
            if candidate is not None and not self.component_identity_conflicts(
                session,
                candidate,
                identity_main_id,
                component_qualifier,
            ):
                return candidate
        radius_deg = self.duplicate_radius_arcsec / 3600.0
        nearby = session.scalars(select(Target).where(
            Target.dec2000_deg.between(
                derived.dec_deg - radius_deg,
                derived.dec_deg + radius_deg,
            )
        ))
        for candidate in nearby:
            position = Astrometry(
                candidate.ra2000_deg, candidate.dec2000_deg,
            )
            if (
                angular_separation_arcsec(position, derived)
                <= self.duplicate_radius_arcsec
                and not self.component_identity_conflicts(
                    session,
                    candidate,
                    identity_main_id,
                    component_qualifier,
                )
            ):
                return candidate
        return None

    @staticmethod
    def target_primary_identity(
        session: Session, target: Target,
    ) -> str | None:
        metadata = session.scalar(
            select(SimbadMetadata)
            .join(MetadataRun, MetadataRun.id == SimbadMetadata.run_id)
            .where(
                SimbadMetadata.target_id == target.id,
                MetadataRun.is_current.is_(True),
                MetadataRun.status == ProviderRunStatus.MATCH,
            )
            .order_by(MetadataRun.id.desc())
            .limit(1)
        )
        if metadata is not None:
            return metadata.main_id
        solution = session.scalar(
            select(AstrometricSolution)
            .where(
                AstrometricSolution.target_id == target.id,
                AstrometricSolution.source == "simbad",
                AstrometricSolution.source_id.is_not(None),
            )
            .order_by(AstrometricSolution.id)
            .limit(1)
        )
        if solution is not None:
            return solution.source_id
        return session.scalar(
            select(Submission.input_name)
            .where(
                Submission.target_id == target.id,
                Submission.input_name.is_not(None),
                Submission.status == "completed",
            )
            .order_by(Submission.id)
            .limit(1)
        )

    def component_identity_conflicts(
        self,
        session: Session,
        target: Target,
        identity_main_id: str | None,
        component_qualifier: str | None,
    ) -> bool:
        if not identity_main_id:
            return False
        current_identity = self.target_primary_identity(session, target)
        if not current_identity:
            return False
        if normalize_identifier(current_identity) == normalize_identifier(
            identity_main_id
        ):
            return False
        current_qualifier = _component_identity_qualifier(current_identity)
        return current_qualifier != component_qualifier and (
            current_qualifier is not None
            or component_qualifier is not None
        )

    @staticmethod
    def available_sdbid(
        session: Session,
        base_sdbid: str,
        *,
        component_qualifier: str | None,
    ) -> str:
        if session.scalar(
            select(Target.id).where(Target.sdbid == base_sdbid)
        ) is None:
            return base_sdbid
        preferred = f"{base_sdbid}-{component_qualifier or 'system'}"
        if session.scalar(
            select(Target.id).where(Target.sdbid == preferred)
        ) is None:
            return preferred
        index = 2
        while session.scalar(select(Target.id).where(
            Target.sdbid == f"{preferred}-{index}"
        )) is not None:
            index += 1
        return f"{preferred}-{index}"

    def rehome_component_identifiers(
        self,
        session: Session,
        target_id: int,
        identifiers: Sequence[tuple[str, str]],
        *,
        component_qualifier: str,
    ) -> None:
        norms = {
            normalize_identifier(value)
            for value, _source in identifiers
            if _component_identity_qualifier(value) == component_qualifier
        }
        if not norms:
            return
        for row in list(session.scalars(select(ExternalIdentifier).where(
            ExternalIdentifier.target_id != target_id,
            ExternalIdentifier.normalized_value.in_(norms),
        ))):
            session.delete(row)

    @staticmethod
    def store_identifiers(
        session: Session,
        target_id: int,
        identifiers: Iterable[tuple[str, str]],
    ) -> None:
        existing = set(session.scalars(
            select(ExternalIdentifier.normalized_value).where(
                ExternalIdentifier.target_id == target_id
            )
        ))
        for value, source in identifiers:
            normalized = normalize_identifier(value)
            if normalized and normalized not in existing:
                session.add(ExternalIdentifier(
                    target_id=target_id,
                    value=value,
                    normalized_value=normalized,
                    source=source,
                ))
                existing.add(normalized)


class IdentityService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        simbad: SimbadProvider | None = None,
        gaia: GaiaProvider | None = None,
        duplicate_radius_arcsec: float = 0.36,
        acceptance_score: float = 0.5,
        acceptance_margin: float = 0.15,
    ):
        self.sessions = session_factory
        self.simbad = simbad or NullSimbad()
        self.gaia = gaia or NullGaia()
        self.duplicate_radius_arcsec = duplicate_radius_arcsec
        self.acceptance_score = acceptance_score
        self.acceptance_margin = acceptance_margin
        self.resolver = IdentityResolver(
            simbad=self.simbad,
            gaia=self.gaia,
            acceptance_score=acceptance_score,
            acceptance_margin=acceptance_margin,
        )
        self.registrar = TargetRegistrar(
            duplicate_radius_arcsec=duplicate_radius_arcsec,
        )

    def add(
        self,
        request: AddRequest,
        *,
        name_resolution: object = _UNSET_RESOLUTION,
    ) -> AddResult:
        request.validate()
        with self.sessions() as session:
            submission = Submission(
                input_name=request.name,
                input_ra_deg=request.ra_deg,
                input_dec_deg=request.dec_deg,
                input_epoch=request.epoch if request.ra_deg is not None else None,
                command=request.command,
                status="running",
            )
            session.add(submission)
            session.flush()
            # Persist the durable attempt before any remote request. This
            # releases SQLite's write lock while SIMBAD and Gaia are queried.
            session.commit()

            resolution = self.resolver.resolve(
                request, name_resolution=name_resolution,
            )
            for outcome in resolution.outcomes:
                self._outcome(
                    session,
                    submission.id,
                    outcome.provider,
                    outcome.status,
                    outcome.message,
                )
            if resolution.selected_astrometry is None:
                submission.status = "failed"
                submission.error = resolution.error
                session.commit()
                raise UnresolvedTarget(
                    submission.error,
                    transient=resolution.transient_failure,
                )
            selected = resolution.selected_astrometry
            identifiers = list(resolution.identifiers)
            candidates = resolution.candidates

            registration = self.registrar.register_target(
                session, submission, resolution,
            )
            target = registration.target
            created = registration.created
            component_qualifier = registration.component_qualifier
            sdbid = target.sdbid

            submission.target_id = target.id
            submission.status = "completed"
            identifiers.append((target.sdbid, "sdb"))
            if created and component_qualifier:
                self.registrar.rehome_component_identifiers(
                    session,
                    target.id,
                    identifiers,
                    component_qualifier=component_qualifier,
                )
            self.registrar.store_identifiers(
                session, target.id, identifiers,
            )
            for value in candidates:
                candidate = value.candidate
                separation = value.separation_arcsec
                score = value.score
                accepted = value.accepted
                row = MatchCandidate(
                    submission_id=submission.id,
                    provider="gaia_dr3",
                    source_id=candidate.source_id,
                    ra_deg=candidate.astrometry.ra_deg,
                    dec_deg=candidate.astrometry.dec_deg,
                    epoch=candidate.astrometry.epoch,
                    pm_ra_cosdec_masyr=candidate.astrometry.pm_ra_cosdec_masyr,
                    pm_dec_masyr=candidate.astrometry.pm_dec_masyr,
                    proper_motion_available=candidate.astrometry.proper_motion_available,
                    parallax_mas=candidate.astrometry.parallax_mas,
                    radial_velocity_kms=candidate.astrometry.radial_velocity_kms,
                    position_bibcode=candidate.astrometry.position_bibcode,
                    proper_motion_bibcode=candidate.astrometry.proper_motion_bibcode,
                    parallax_bibcode=candidate.astrometry.parallax_bibcode,
                    radial_velocity_bibcode=candidate.astrometry.radial_velocity_bibcode,
                    separation_arcsec=separation,
                    score=score,
                    score_details=json.dumps({
                        "separation_arcsec": separation,
                        "scale_arcsec": 2.0,
                        "simbad_gaia_dr3_ids": sorted(
                            gaia_dr3_identifiers_from_pairs(identifiers)
                        ),
                        "gaia_identifier_agreement": value.identifier_agreement,
                    }),
                )
                session.add(row)
                session.flush()
                if accepted:
                    session.add(MatchDecision(candidate_id=row.id, decision="accepted", method="automatic", reason="score and margin thresholds met"))
                else:
                    session.add(MatchDecision(candidate_id=row.id, decision="deferred", method="automatic", reason="score or margin threshold not met"))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                with self.sessions() as retry:
                    target = retry.scalar(select(Target).where(Target.sdbid == sdbid))
                    if target is None:
                        raise
                    return AddResult(target.id, target.sdbid, False, selected.source)
            return AddResult(target.id, target.sdbid, created, selected.source)

    def override_match(
        self,
        candidate_id: int,
        *,
        actor: str | None,
        reason: str | None = None,
    ) -> None:
        with self.sessions.begin() as session:
            candidate = session.get(MatchCandidate, candidate_id)
            if candidate is None:
                raise KeyError(f"candidate {candidate_id} not found")
            decision = DecisionContext.resolve(
                actor=actor,
                reason=reason,
                suggested_reason=(
                    f"Accepted identity candidate {candidate.id} from "
                    f"{candidate.provider} source {candidate.source_id}"
                ),
            )
            submission = session.get(Submission, candidate.submission_id)
            if submission is None or submission.target_id is None:
                raise ValueError(
                    "identity candidate is not attached to a target"
                )
            target = session.get(Target, submission.target_id)
            if target is None:
                raise ValueError(
                    "identity candidate target no longer exists"
                )
            selected_ids = effective_identity_candidate_ids(
                session, submission_ids=[submission.id],
            )
            astrometry = Astrometry(
                candidate.ra_deg,
                candidate.dec_deg,
                candidate.epoch,
                pm_ra_cosdec_masyr=candidate.pm_ra_cosdec_masyr,
                pm_dec_masyr=candidate.pm_dec_masyr,
                parallax_mas=candidate.parallax_mas,
                radial_velocity_kms=candidate.radial_velocity_kms,
                source=candidate.provider,
                source_id=candidate.source_id,
                position_bibcode=candidate.position_bibcode,
                proper_motion_bibcode=candidate.proper_motion_bibcode,
                parallax_bibcode=candidate.parallax_bibcode,
                radial_velocity_bibcode=candidate.radial_velocity_bibcode,
            )
            derived = propagate_to_epoch(astrometry, 2000.0)
            existing_solution = session.scalar(
                select(AstrometricSolution)
                .where(
                    AstrometricSolution.target_id == target.id,
                    AstrometricSolution.source == candidate.provider,
                    AstrometricSolution.source_id == candidate.source_id,
                    AstrometricSolution.ra_deg == candidate.ra_deg,
                    AstrometricSolution.dec_deg == candidate.dec_deg,
                    AstrometricSolution.epoch == candidate.epoch,
                )
                .order_by(AstrometricSolution.id.desc())
                .limit(1)
            )
            if existing_solution is None:
                existing_solution = _astrometric_solution(
                    target.id,
                    astrometry,
                    derived_ra2000_deg=derived.ra_deg,
                    derived_dec2000_deg=derived.dec_deg,
                )
                session.add(existing_solution)
                session.flush()
            canonical_changed = (
                target.canonical_astrometry_id != existing_solution.id
            )
            position_changed = (
                target.ra2000_deg != derived.ra_deg
                or target.dec2000_deg != derived.dec_deg
            )
            target.canonical_astrometry_id = existing_solution.id
            target.ra2000_deg = derived.ra_deg
            target.dec2000_deg = derived.dec_deg
            identifier = (
                f"Gaia DR3 {candidate.source_id}"
                if candidate.provider == "gaia_dr3"
                else f"{candidate.provider} {candidate.source_id}"
            )
            normalized_identifier = normalize_identifier(identifier)
            identifier_present = session.scalar(
                select(ExternalIdentifier.id)
                .where(
                    ExternalIdentifier.target_id == target.id,
                    ExternalIdentifier.normalized_value
                    == normalized_identifier,
                )
                .limit(1)
            ) is not None
            self.registrar.store_identifiers(
                session, target.id, [(identifier, candidate.provider)],
            )
            if (
                candidate.id in selected_ids
                and not canonical_changed
                and not position_changed
                and identifier_present
            ):
                return
            session.add(MatchDecision(
                candidate_id=candidate_id,
                decision="accepted",
                method="manual",
                actor=decision.actor,
                reason=decision.reason,
            ))
            session.flush()
            mark_export_dirty(
                session,
                target.id,
                source_type="identity_match_decision",
                source_id=candidate.id,
                reason="manual identity candidate acceptance",
            )

    def match_history(self, candidate_id: int) -> list[MatchDecision]:
        with self.sessions() as session:
            return list(session.scalars(select(MatchDecision).where(MatchDecision.candidate_id == candidate_id).order_by(MatchDecision.id)))

    @staticmethod
    def _outcome(
        session,
        submission_id,
        provider,
        status: str | ProviderRunStatus,
        message=None,
    ):
        value = ProviderRunStatus.parse(status, "provider status")
        session.add(ProviderOutcome(
            submission_id=submission_id,
            provider=provider,
            status=value.value,
            message=message,
        ))

    @staticmethod
    def _error_status(error: ProviderError) -> ProviderRunStatus:
        return (
            ProviderRunStatus.TRANSIENT_FAILURE
            if error.transient
            else ProviderRunStatus.PERMANENT_FAILURE
        )


def identifiers_from_pairs(values: Sequence[tuple[str, str]]) -> list[str]:
    return [value for value, _source in values]


def gaia_dr3_identifiers_from_pairs(values: Sequence[tuple[str, str]]) -> set[str]:
    result = set()
    for value, source in values:
        if source != "simbad":
            continue
        matched = _GAIA_DR3_IDENTIFIER.match(value.strip())
        if matched:
            result.add(matched.group(1))
    return result
