from __future__ import annotations

import json
import math
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone

from astropy.time import Time
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from .adapters.vizier import row_float, row_text
from .alma_transport import AlmaArchiveProvider
from .models.alma import AlmaMember, AlmaMemberPosition, AlmaSyncChunk, AlmaSyncRun


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

class AlmaSyncService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: AlmaArchiveProvider,
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
            member_key = (proposal_id, archive_member_uid)
            value = aggregates.setdefault(member_key, {
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
            for (proposal_id, member_ous_uid), aggregate in aggregates.items():
                member = session.scalar(select(AlmaMember).where(
                    AlmaMember.proposal_id == proposal_id,
                    AlmaMember.member_ous_uid == member_ous_uid,
                ))
                same_run = member is not None and member.last_seen_run_id == run_id
                existing_positions = []
                if same_run:
                    existing_positions = [
                        {
                            "ra_deg": value.ra_deg,
                            "dec_deg": value.dec_deg,
                            "fov_deg": value.fov_deg,
                            "region": value.region,
                        }
                        for value in session.scalars(
                            select(AlmaMemberPosition).where(
                                AlmaMemberPosition.member_id == member.id
                            )
                        )
                    ]
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
                        member_ous_uid=member_ous_uid,
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
