from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .astrometry import angular_separation_arcsec
from .models.identity import AstrometricSolution, Target
from .models.metadata import MetadataRun, SimbadMetadata
from .providers import Astrometry


DEFAULT_METADATA_AGREEMENT_ARCSEC = 10.0


def best_target_astrometry(
    session: Session,
    target: Target,
    *,
    metadata_agreement_arcsec: float = DEFAULT_METADATA_AGREEMENT_ARCSEC,
) -> Astrometry:
    """Return the best current astrometry available for geometric work.

    Canonical identity astrometry remains authoritative.  When it has no
    proper motion, a position-consistent current SIMBAD metadata result can
    supply the motion needed to compare observations at other epochs.
    """
    return best_target_astrometry_map(
        session,
        (target,),
        metadata_agreement_arcsec=metadata_agreement_arcsec,
    )[target.id]


def best_target_astrometry_map(
    session: Session,
    targets: Iterable[Target],
    *,
    metadata_agreement_arcsec: float = DEFAULT_METADATA_AGREEMENT_ARCSEC,
) -> dict[int, Astrometry]:
    targets = tuple(targets)
    if not targets:
        return {}

    solution_ids = {
        target.canonical_astrometry_id
        for target in targets
        if target.canonical_astrometry_id is not None
    }
    solutions = {
        solution.id: solution
        for solution in session.scalars(
            select(AstrometricSolution).where(
                AstrometricSolution.id.in_(solution_ids)
            )
        )
    }
    base_by_target = {
        target.id: _canonical_value(
            target, solutions.get(target.canonical_astrometry_id)
        )
        for target in targets
    }
    missing_motion_ids = tuple(
        target_id
        for target_id, value in base_by_target.items()
        if not value.proper_motion_available
    )
    if not missing_motion_ids:
        return base_by_target

    metadata_by_target: dict[int, SimbadMetadata] = {}
    for metadata in session.scalars(
        select(SimbadMetadata)
        .join(MetadataRun, MetadataRun.id == SimbadMetadata.run_id)
        .where(
            SimbadMetadata.target_id.in_(missing_motion_ids),
            SimbadMetadata.pm_ra_cosdec_masyr.is_not(None),
            SimbadMetadata.pm_dec_masyr.is_not(None),
            MetadataRun.is_current.is_(True),
            MetadataRun.status == "match",
        )
        .order_by(SimbadMetadata.target_id, SimbadMetadata.id.desc())
    ):
        metadata_by_target.setdefault(metadata.target_id, metadata)

    result = dict(base_by_target)
    for target_id, metadata in metadata_by_target.items():
        candidate = _metadata_value(metadata)
        base = base_by_target[target_id]
        if (
            angular_separation_arcsec(base, candidate, epoch=base.epoch)
            <= metadata_agreement_arcsec
        ):
            result[target_id] = candidate
    return result


def _canonical_value(
    target: Target,
    solution: AstrometricSolution | None,
) -> Astrometry:
    if solution is None:
        return Astrometry(
            target.ra2000_deg,
            target.dec2000_deg,
            2000.0,
            source="sdb",
            source_id=target.sdbid,
        )
    return Astrometry(
        solution.ra_deg,
        solution.dec_deg,
        solution.epoch,
        pm_ra_cosdec_masyr=solution.pm_ra_cosdec_masyr,
        pm_dec_masyr=solution.pm_dec_masyr,
        parallax_mas=solution.parallax_mas,
        radial_velocity_kms=solution.radial_velocity_kms,
        source=solution.source,
        source_id=solution.source_id,
        position_bibcode=solution.position_bibcode,
        proper_motion_bibcode=solution.proper_motion_bibcode,
        parallax_bibcode=solution.parallax_bibcode,
        radial_velocity_bibcode=solution.radial_velocity_bibcode,
    )


def _metadata_value(metadata: SimbadMetadata) -> Astrometry:
    return Astrometry(
        metadata.ra_deg,
        metadata.dec_deg,
        2000.0,
        pm_ra_cosdec_masyr=metadata.pm_ra_cosdec_masyr,
        pm_dec_masyr=metadata.pm_dec_masyr,
        parallax_mas=metadata.parallax_mas,
        radial_velocity_kms=metadata.radial_velocity_kms,
        source="simbad metadata",
        source_id=metadata.main_id,
        proper_motion_bibcode=metadata.proper_motion_bibcode,
        parallax_bibcode=metadata.parallax_bibcode,
        radial_velocity_bibcode=metadata.radial_velocity_bibcode,
    )
