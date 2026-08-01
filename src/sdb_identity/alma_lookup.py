"""Motion-aware local lookup over canonical ALMA member pointings."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from astropy.time import Time
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .astrometry import angular_separation_arcsec, propagate_to_epoch
from .models.alma import AlmaMember, AlmaMemberPosition
from .models.identity import AstrometricSolution
from .providers import Astrometry
from .targets import resolve_target


@dataclass(frozen=True)
class AlmaProject:
    proposal_id: str
    observation_count: int
    band_lists: tuple[str, ...]


class AlmaLookupService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.sessions = session_factory

    def projects(
        self,
        target_reference: str | int,
        radius_arcsec: float = 10.0,
    ) -> tuple[AlmaProject, ...]:
        if radius_arcsec <= 0:
            raise ValueError("radius must be positive")
        with self.sessions() as session:
            target = resolve_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            solution = session.get(
                AstrometricSolution,
                target.canonical_astrometry_id,
            )
            native = Astrometry(
                solution.ra_deg,
                solution.dec_deg,
                solution.epoch,
                solution.pm_ra_cosdec_masyr,
                solution.pm_dec_masyr,
                source=solution.source,
                source_id=solution.source_id,
            )
            track_margin_arcsec = max(
                angular_separation_arcsec(
                    Astrometry(native.ra_deg, native.dec_deg, epoch),
                    propagate_to_epoch(native, epoch),
                    epoch=epoch,
                )
                for epoch in (
                    2010.0,
                    float(datetime.now(timezone.utc).year + 1),
                )
            )
            margin_deg = track_margin_arcsec / 3600.0 + radius_arcsec / 3600.0
            normal_fov_limit = 2.0
            row_radius = func.max(
                radius_arcsec / 3600.0,
                func.coalesce(AlmaMemberPosition.fov_deg / 2.0, 0.0),
            ) + track_margin_arcsec / 3600.0
            ra_delta = func.abs(AlmaMemberPosition.ra_deg - native.ra_deg)
            wrapped_ra_delta = func.min(ra_delta, 360.0 - ra_delta)
            cos_dec = max(abs(math.cos(math.radians(native.dec_deg))), 0.01)
            exact = (
                func.abs(AlmaMemberPosition.dec_deg - native.dec_deg)
                <= row_radius,
                wrapped_ra_delta <= row_radius / cos_dec,
            )
            fixed_radius = normal_fov_limit / 2.0 + margin_deg
            normal = list(
                session.execute(
                    select(AlmaMemberPosition, AlmaMember)
                    .join(
                        AlmaMember,
                        AlmaMember.id == AlmaMemberPosition.member_id,
                    )
                    .where(
                        AlmaMember.active.is_(True),
                        func.coalesce(AlmaMemberPosition.fov_deg, 0.0)
                        <= normal_fov_limit,
                        AlmaMemberPosition.dec_deg.between(
                            native.dec_deg - fixed_radius,
                            native.dec_deg + fixed_radius,
                        ),
                        wrapped_ra_delta <= fixed_radius / cos_dec,
                        *exact,
                    )
                )
            )
            large = list(
                session.execute(
                    select(AlmaMemberPosition, AlmaMember)
                    .join(
                        AlmaMember,
                        AlmaMember.id == AlmaMemberPosition.member_id,
                    )
                    .where(
                        AlmaMember.active.is_(True),
                        AlmaMemberPosition.fov_deg > normal_fov_limit,
                        *exact,
                    )
                )
            )
            candidates = (*normal, *large)

        projects: dict[str, dict] = {}
        astrometry_by_member = {}
        matched_members = set()
        for position_value, member in candidates:
            if member.id in matched_members:
                continue
            if member.t_min_mjd is not None:
                midpoint = member.t_min_mjd
                if member.t_max_mjd is not None:
                    midpoint = (midpoint + member.t_max_mjd) / 2.0
                epoch = float(Time(midpoint, format="mjd").jyear)
            else:
                epoch = native.epoch
            moved = astrometry_by_member.setdefault(
                member.id,
                propagate_to_epoch(native, epoch),
            )
            position = Astrometry(
                position_value.ra_deg,
                position_value.dec_deg,
                epoch,
                source="alma",
            )
            separation = angular_separation_arcsec(moved, position, epoch=epoch)
            fov = position_value.fov_deg
            footprint_radius = fov * 1800.0 if fov and fov > 0 else 0.0
            if separation > max(radius_arcsec, footprint_radius):
                continue
            matched_members.add(member.id)
            value = projects.setdefault(
                member.proposal_id,
                {"count": 0, "bands": set()},
            )
            value["count"] += 1
            if member.band_list:
                value["bands"].update(member.band_list.split())
        return tuple(
            AlmaProject(code, value["count"], tuple(sorted(value["bands"])))
            for code, value in sorted(projects.items())
        )
