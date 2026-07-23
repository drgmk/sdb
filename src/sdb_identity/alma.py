from __future__ import annotations

import json
import math
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol

from astropy.time import Time
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .adapters.vizier import row_float, row_payload, row_text
from .astrometry import angular_separation_arcsec, propagate_to_epoch
from .dirty import find_target
from .models import (
    AlmaMember, AlmaMemberPosition, AlmaObservation, AlmaSyncChunk, AlmaSyncRun,
    AstrometricSolution,
)
from .providers import Astrometry, ProviderError


ALMA_COLUMNS = (
    "obs_publisher_did", "obs_id", "group_ous_uid", "member_ous_uid",
    "asdm_uid", "proposal_id", "target_name", "s_ra", "s_dec",
    "s_fov", "s_region", "t_min", "t_max", "obs_release_date",
    "data_rights", "band_list", "lastModified",
)


class AlmaArchiveProvider(Protocol):
    archive_url: str

    def bootstrap_chunk(self, start_mjd: float, end_mjd: float): ...
    def bootstrap_undated(self): ...
    def modified_since(self, watermark: str): ...


class AstroqueryAlmaArchive:
    """Thin ALMA ObsCore client; TAP returns public and proprietary metadata."""

    mirrors = (
        "https://almascience.org",
        "https://almascience.eso.org",
        "https://almascience.nrao.edu",
        "https://almascience.nao.ac.jp",
    )

    def __init__(self, archive_url: str | None = None, timeout_seconds: float = 300):
        from astroquery.alma import Alma

        self.client = Alma()
        if archive_url:
            self.client.archive_url = archive_url
        self.archive_url = str(self.client.archive_url)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _projection():
        return ", ".join(ALMA_COLUMNS)

    def bootstrap_chunk(self, start_mjd: float, end_mjd: float):
        query = (
            f"SELECT {self._projection()} FROM ivoa.obscore "
            f"WHERE t_min >= {start_mjd:.8f} AND t_min < {end_mjd:.8f}"
        )
        return self._query(query)

    def bootstrap_undated(self):
        query = (
            f"SELECT {self._projection()} FROM ivoa.obscore WHERE t_min IS NULL"
        )
        return self._query(query)

    def modified_since(self, watermark: str):
        safe = watermark.replace("'", "''")
        query = (
            f"SELECT {self._projection()} FROM ivoa.obscore "
            f"WHERE lastModified > '{safe}'"
        )
        return self._query(query)

    def _query(self, query):
        from astroquery.alma import Alma

        urls = tuple(dict.fromkeys((self.archive_url, *self.mirrors)))
        failures = []
        for url in urls:
            client = self.client
            if str(client.archive_url) != url:
                client = Alma()
                client.archive_url = url
            try:
                job = client.tap.submit_job(query)
                try:
                    job.run()
                    job.wait(
                        phases={"COMPLETED", "ERROR", "ABORTED"},
                        timeout=self.timeout_seconds,
                    )
                    if job.phase != "COMPLETED":
                        raise RuntimeError(f"ALMA TAP job ended in phase {job.phase}")
                    result = job.fetch_result().to_table()
                except BaseException:
                    try:
                        job.abort()
                    except Exception:
                        pass
                    raise
                finally:
                    try:
                        job.delete()
                    except Exception:
                        pass
                self.client = client
                self.archive_url = url
                return result
            except Exception as error:
                failures.append(f"{url}: {error}")
        raise ProviderError(
            "ALMA TAP query failed on every mirror: " + " | ".join(failures),
            transient=True,
        )


@dataclass(frozen=True)
class AlmaSyncSummary:
    run_id: int
    mode: str
    status: str
    row_count: int
    upserted_count: int
    deactivated_count: int
    watermark_before: str | None
    watermark_after: str | None


@dataclass(frozen=True)
class AlmaProject:
    proposal_id: str
    observation_count: int
    band_lists: tuple[str, ...]


class AlmaArchiveService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: AlmaArchiveProvider | None,
    ):
        self.sessions = session_factory
        self.provider = provider

    def bootstrap(
        self,
        start_year: int = 2011,
        end_year: int | None = None,
        chunk_months: int = 3,
    ):
        end_year = end_year or (datetime.now(timezone.utc).year + 1)
        if end_year <= start_year:
            raise ValueError("end year must be after start year")
        if not 1 <= chunk_months <= 12:
            raise ValueError("chunk months must be between 1 and 12")
        run = self._start("bootstrap", None)
        try:
            self._create_bootstrap_chunks(
                run.id, start_year, end_year, chunk_months,
            )
            return self._execute_bootstrap(run.id)
        except BaseException as error:
            self._fail(run.id, error)
            raise

    def resume(
        self,
        run_id: int,
        *,
        start_year: int = 2011,
        end_year: int | None = None,
        chunk_months: int = 3,
    ):
        end_year = end_year or (datetime.now(timezone.utc).year + 1)
        with self.sessions.begin() as session:
            run = session.get(AlmaSyncRun, run_id)
            if run is None:
                raise KeyError(f"ALMA sync run not found: {run_id}")
            if run.mode != "bootstrap":
                raise ValueError("only bootstrap ALMA runs can currently be resumed")
            run.status = "running"
            run.error = None
            run.completed_at = None
        with self.sessions() as session:
            has_chunks = session.scalar(select(AlmaSyncChunk.id).where(
                AlmaSyncChunk.run_id == run_id
            ).limit(1)) is not None
        if not has_chunks:
            self._create_bootstrap_chunks(
                run_id, start_year, end_year, chunk_months,
            )
            self._adopt_legacy_completed_chunks(run_id)
        try:
            return self._execute_bootstrap(run_id)
        except BaseException as error:
            self._fail(run_id, error)
            raise

    def _create_bootstrap_chunks(self, run_id, start_year, end_year, chunk_months):
        chunk_start = date(start_year, 1, 1)
        archive_end = date(end_year, 1, 1)
        chunks = []
        while chunk_start < archive_end:
            month_index = chunk_start.year * 12 + chunk_start.month - 1 + chunk_months
            chunk_end = date(month_index // 12, month_index % 12 + 1, 1)
            chunk_end = min(chunk_end, archive_end)
            chunks.append(AlmaSyncChunk(
                run_id=run_id,
                label=f"{chunk_start.isoformat()}_{chunk_end.isoformat()}",
                kind="dated",
                start_mjd=float(Time(chunk_start.isoformat()).mjd),
                end_mjd=float(Time(chunk_end.isoformat()).mjd),
                status="pending",
            ))
            chunk_start = chunk_end
        chunks.append(AlmaSyncChunk(
            run_id=run_id, label="undated", kind="undated", status="pending",
        ))
        with self.sessions.begin() as session:
            session.add_all(chunks)

    def _adopt_legacy_completed_chunks(self, run_id):
        with self.sessions.begin() as session:
            max_seen = session.scalar(select(func.max(AlmaObservation.t_min_mjd)).where(
                AlmaObservation.last_seen_run_id == run_id
            ))
            if max_seen is None:
                return
            for chunk in session.scalars(select(AlmaSyncChunk).where(
                AlmaSyncChunk.run_id == run_id,
                AlmaSyncChunk.kind == "dated",
            ).order_by(AlmaSyncChunk.start_mjd)):
                if chunk.start_mjd <= max_seen:
                    chunk.status = "completed"
                    chunk.completed_at = datetime.now(timezone.utc)

    def _execute_bootstrap(self, run_id):
        with self.sessions() as session:
            chunk_ids = list(session.scalars(
                select(AlmaSyncChunk.id)
                .where(
                    AlmaSyncChunk.run_id == run_id,
                    AlmaSyncChunk.status != "completed",
                )
                .order_by(AlmaSyncChunk.id)
            ))
        for chunk_id in chunk_ids:
            with self.sessions.begin() as session:
                chunk = session.get(AlmaSyncChunk, chunk_id)
                chunk.status = "running"
                chunk.attempts += 1
                chunk.error = None
                chunk.started_at = datetime.now(timezone.utc)
                kind, start_mjd, end_mjd = chunk.kind, chunk.start_mjd, chunk.end_mjd
            try:
                rows = (
                    self.provider.bootstrap_chunk(start_mjd, end_mjd)
                    if kind == "dated"
                    else self.provider.bootstrap_undated()
                )
                count = self._store_rows(run_id, rows)
                with self.sessions.begin() as session:
                    chunk = session.get(AlmaSyncChunk, chunk_id)
                    chunk.status = "completed"
                    chunk.row_count = count
                    chunk.archive_url = self.provider.archive_url
                    chunk.completed_at = datetime.now(timezone.utc)
            except BaseException as error:
                with self.sessions.begin() as session:
                    chunk = session.get(AlmaSyncChunk, chunk_id)
                    chunk.status = "failed"
                    chunk.archive_url = self.provider.archive_url
                    chunk.error = str(error)
                    chunk.completed_at = datetime.now(timezone.utc)
                raise
        with self.sessions.begin() as session:
            result = session.execute(
                update(AlmaMember)
                .where(
                    AlmaMember.active.is_(True),
                    AlmaMember.last_seen_run_id != run_id,
                )
                .values(active=False)
            )
            session.get(AlmaSyncRun, run_id).deactivated_count = result.rowcount
        return self._finish(run_id)

    def incremental(self):
        with self.sessions() as session:
            previous = session.scalar(
                select(AlmaSyncRun)
                .where(
                    AlmaSyncRun.status == "completed",
                    AlmaSyncRun.watermark_after.is_not(None),
                )
                .order_by(AlmaSyncRun.id.desc())
                .limit(1)
            )
        if previous is None:
            raise ValueError("no ALMA watermark; run a bootstrap first")
        run = self._start("incremental", previous.watermark_after)
        try:
            self._store_rows(
                run.id, self.provider.modified_since(previous.watermark_after)
            )
            return self._finish(run.id)
        except BaseException as error:
            self._fail(run.id, error)
            raise

    def compact_observations(self):
        """Convert the initial product-row cache to one row per member OUS."""
        run = self._start("compact", None)
        try:
            with self.sessions() as session:
                observations = session.scalars(
                    select(AlmaObservation).where(AlmaObservation.active.is_(True))
                ).yield_per(5000)

                def provider_rows():
                    for value in observations:
                        payload = json.loads(value.payload_json)
                        payload.update({
                            "proposal_id": value.proposal_id,
                            "target_name": value.target_name,
                            "s_ra": value.ra_deg,
                            "s_dec": value.dec_deg,
                            "s_fov": value.fov_deg,
                            "s_region": value.region,
                            "t_min": value.t_min_mjd,
                            "t_max": value.t_max_mjd,
                            "obs_release_date": value.release_date,
                            "data_rights": value.data_rights,
                            "band_list": value.band_list,
                            "lastModified": value.last_modified,
                        })
                        yield payload

                self._store_rows(run.id, provider_rows())
            with self.sessions.begin() as session:
                session.execute(delete(AlmaObservation))
            return self._finish(run.id)
        except BaseException as error:
            self._fail(run.id, error)
            raise

    def rebuild_member_bounds(self):
        updated = 0
        with self.sessions.begin() as session:
            for member in session.scalars(select(AlmaMember)):
                center_ra, center_dec, radius = self._member_bounds(
                    json.loads(member.positions_json)
                )
                member.center_ra_deg = center_ra
                member.center_dec_deg = center_dec
                member.bounding_radius_deg = radius
                updated += 1
        return updated

    def rebuild_member_positions(self):
        inserted = 0
        with self.sessions.begin() as session:
            session.execute(delete(AlmaMemberPosition))
            for member in session.scalars(select(AlmaMember)).yield_per(1000):
                for value in json.loads(member.positions_json):
                    session.add(self._position_model(member.id, value))
                    inserted += 1
                    if inserted % 5000 == 0:
                        session.flush()
        return inserted

    def projects(self, target_reference, radius_arcsec: float = 10.0):
        if radius_arcsec <= 0:
            raise ValueError("radius must be positive")
        with self.sessions() as session:
            target = find_target(session, target_reference)
            if target is None:
                raise KeyError(f"target not found: {target_reference}")
            solution = session.get(AstrometricSolution, target.canonical_astrometry_id)
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
                for epoch in (2010.0, float(datetime.now(timezone.utc).year + 1))
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
                func.abs(AlmaMemberPosition.dec_deg - native.dec_deg) <= row_radius,
                wrapped_ra_delta <= row_radius / cos_dec,
            )
            fixed_radius = normal_fov_limit / 2.0 + margin_deg
            normal = list(session.execute(
                select(AlmaMemberPosition, AlmaMember)
                .join(AlmaMember, AlmaMember.id == AlmaMemberPosition.member_id)
                .where(
                    AlmaMember.active.is_(True),
                    func.coalesce(AlmaMemberPosition.fov_deg, 0.0) <= normal_fov_limit,
                    AlmaMemberPosition.dec_deg.between(
                        native.dec_deg - fixed_radius,
                        native.dec_deg + fixed_radius,
                    ),
                    wrapped_ra_delta <= fixed_radius / cos_dec,
                    *exact,
                )
            ))
            large = list(session.execute(
                select(AlmaMemberPosition, AlmaMember)
                .join(AlmaMember, AlmaMember.id == AlmaMemberPosition.member_id)
                .where(
                    AlmaMember.active.is_(True),
                    AlmaMemberPosition.fov_deg > normal_fov_limit,
                    *exact,
                )
            ))
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
                member.id, propagate_to_epoch(native, epoch)
            )
            position = Astrometry(
                position_value.ra_deg, position_value.dec_deg, epoch, source="alma"
            )
            separation = angular_separation_arcsec(moved, position, epoch=epoch)
            fov = position_value.fov_deg
            footprint_radius = fov * 1800.0 if fov and fov > 0 else 0.0
            if separation > max(radius_arcsec, footprint_radius):
                continue
            matched_members.add(member.id)
            value = projects.setdefault(member.proposal_id, {
                "count": 0, "bands": set(),
            })
            value["count"] += 1
            if member.band_list:
                value["bands"].update(member.band_list.split())
        return tuple(
            AlmaProject(code, value["count"], tuple(sorted(value["bands"])))
            for code, value in sorted(projects.items())
        )

    def _start(self, mode, watermark):
        with self.sessions.begin() as session:
            run = AlmaSyncRun(
                mode=mode,
                archive_url=self.provider.archive_url,
                status="running",
                watermark_before=watermark,
            )
            session.add(run)
            session.flush()
            return run

    def _store_rows(self, run_id, rows):
        aggregates = {}
        row_count = 0
        for row in rows:
            archive_member_uid = row_text(row, "member_ous_uid")
            proposal_id = row_text(row, "proposal_id")
            ra = row_float(row, "s_ra")
            dec = row_float(row, "s_dec")
            if not archive_member_uid or not proposal_id or ra is None or dec is None:
                raise ValueError("ALMA row omitted member OUS, proposal ID, or position")
            # Two early-science member UIDs are reused by different proposals.
            # The proposal-qualified value is unique across the live archive.
            member_uid = f"{proposal_id}|{archive_member_uid}"
            value = aggregates.setdefault(member_uid, {
                "proposal_id": proposal_id,
                "targets": set(),
                "positions": {},
                "bands": set(),
                "rights": set(),
                "t_min": None,
                "t_max": None,
                "release": None,
                "modified": None,
            })
            target = row_text(row, "target_name")
            if target:
                value["targets"].add(target)
            fov, region = row_float(row, "s_fov"), row_text(row, "s_region")
            key = (round(ra % 360.0, 9), round(dec, 9), fov, region)
            value["positions"][key] = {
                "ra_deg": ra % 360.0,
                "dec_deg": dec,
                "fov_deg": fov,
                "region": region,
            }
            bands = row_text(row, "band_list")
            if bands:
                value["bands"].update(bands.split())
            rights = row_text(row, "data_rights")
            if rights:
                value["rights"].add(rights)
            t_min, t_max = row_float(row, "t_min"), row_float(row, "t_max")
            if t_min is not None:
                value["t_min"] = t_min if value["t_min"] is None else min(value["t_min"], t_min)
            if t_max is not None:
                value["t_max"] = t_max if value["t_max"] is None else max(value["t_max"], t_max)
            for source, key_name in (
                (row_text(row, "obs_release_date"), "release"),
                (row_text(row, "lastModified", "last_modified"), "modified"),
            ):
                if source and (value[key_name] is None or source > value[key_name]):
                    value[key_name] = source
            row_count += 1

        upserted = 0
        with self.sessions.begin() as session:
            run = session.get(AlmaSyncRun, run_id)
            run.archive_url = self.provider.archive_url
            for member_uid, aggregate in aggregates.items():
                member = session.scalar(select(AlmaMember).where(
                    AlmaMember.member_ous_uid == member_uid
                ))
                same_run = member is not None and member.last_seen_run_id == run_id
                existing_positions = (
                    json.loads(member.positions_json) if same_run else []
                )
                positions = {
                    (
                        round(value["ra_deg"], 9), round(value["dec_deg"], 9),
                        value.get("fov_deg"), value.get("region"),
                    ): value
                    for value in existing_positions
                }
                positions.update(aggregate["positions"])
                targets = set(json.loads(member.target_names_json)) if same_run else set()
                bands = set(member.band_list.split()) if same_run and member.band_list else set()
                rights = set(member.data_rights.split()) if same_run and member.data_rights else set()
                values = {
                    "proposal_id": aggregate["proposal_id"],
                    "target_names_json": json.dumps(sorted(targets | aggregate["targets"])),
                    "positions_json": json.dumps(list(positions.values()), sort_keys=True),
                    "t_min_mjd": self._minimum(member.t_min_mjd if same_run else None, aggregate["t_min"]),
                    "t_max_mjd": self._maximum(member.t_max_mjd if same_run else None, aggregate["t_max"]),
                    "release_date": self._maximum(member.release_date if same_run else None, aggregate["release"]),
                    "data_rights": " ".join(sorted(rights | aggregate["rights"])),
                    "band_list": " ".join(sorted(bands | aggregate["bands"])),
                    "last_modified": self._maximum(member.last_modified if same_run else None, aggregate["modified"]),
                    "last_seen_run_id": run_id,
                    "active": True,
                }
                center_ra, center_dec, radius = self._member_bounds(
                    list(positions.values())
                )
                values.update({
                    "center_ra_deg": center_ra,
                    "center_dec_deg": center_dec,
                    "bounding_radius_deg": radius,
                })
                if member is None:
                    member = AlmaMember(
                        member_ous_uid=member_uid,
                        first_seen_run_id=run_id,
                        **values,
                    )
                    session.add(member)
                    session.flush()
                else:
                    for key, value in values.items():
                        setattr(member, key, value)
                if not same_run:
                    session.execute(delete(AlmaMemberPosition).where(
                        AlmaMemberPosition.member_id == member.id
                    ))
                existing_keys = set(session.scalars(
                    select(AlmaMemberPosition.position_key).where(
                        AlmaMemberPosition.member_id == member.id
                    )
                ))
                for position_value in positions.values():
                    model = self._position_model(member.id, position_value)
                    if model.position_key not in existing_keys:
                        session.add(model)
                upserted += 1
                if aggregate["modified"] and (
                    run.watermark_after is None
                    or aggregate["modified"] > run.watermark_after
                ):
                    run.watermark_after = aggregate["modified"]
            run.row_count += row_count
            run.upserted_count += upserted
        return row_count

    @staticmethod
    def _minimum(first, second):
        values = [value for value in (first, second) if value is not None]
        return min(values) if values else None

    @staticmethod
    def _maximum(first, second):
        values = [value for value in (first, second) if value is not None]
        return max(values) if values else None

    @staticmethod
    def _member_bounds(positions):
        if not positions:
            return None, None, None
        center_ra = float(positions[0]["ra_deg"]) % 360.0
        center_dec = float(positions[0]["dec_deg"])
        radius = 0.0
        ra1, dec1 = math.radians(center_ra), math.radians(center_dec)
        for value in positions:
            ra2, dec2 = math.radians(value["ra_deg"]), math.radians(value["dec_deg"])
            haversine = (
                math.sin((dec2 - dec1) / 2.0) ** 2
                + math.cos(dec1) * math.cos(dec2)
                * math.sin((ra2 - ra1) / 2.0) ** 2
            )
            separation = math.degrees(2.0 * math.asin(min(1.0, math.sqrt(haversine))))
            fov_radius = (value.get("fov_deg") or 0.0) / 2.0
            radius = max(radius, separation + fov_radius)
        return center_ra, center_dec, radius

    @staticmethod
    def _position_model(member_id, value):
        identity = json.dumps(
            [
                round(value["ra_deg"] % 360.0, 9), round(value["dec_deg"], 9),
                value.get("fov_deg"), value.get("region"),
            ],
            separators=(",", ":"),
        )
        return AlmaMemberPosition(
            member_id=member_id,
            position_key=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            ra_deg=value["ra_deg"] % 360.0,
            dec_deg=value["dec_deg"],
            fov_deg=value.get("fov_deg"),
            region=value.get("region"),
        )

    def _finish(self, run_id):
        with self.sessions.begin() as session:
            run = session.get(AlmaSyncRun, run_id)
            if run.watermark_after is None:
                run.watermark_after = run.watermark_before
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
        return AlmaSyncSummary(
            run.id, run.mode, run.status, run.row_count, run.upserted_count,
            run.deactivated_count, run.watermark_before, run.watermark_after,
        )

    def _fail(self, run_id, error):
        with self.sessions.begin() as session:
            run = session.get(AlmaSyncRun, run_id)
            run.status = "failed"
            run.error = str(error)
            run.completed_at = datetime.now(timezone.utc)
