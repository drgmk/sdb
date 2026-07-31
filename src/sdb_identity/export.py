"""Pure target export projection and SDF-compatible IPAC serialization."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from astropy.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .dirty import (
    export_dirty_watermark,
    mark_exported_through,
)
from .measurement_eligibility import EffectiveMeasurementEligibility
from .models import (
    ExternalIdentifier,
    MetadataRun,
    NormalizedMeasurement,
    SimbadMetadata,
    Target,
)
from .system_photometry import load_system_photometry_state
from .targets import resolve_target
from .vocabulary import ProviderRunStatus


@dataclass(frozen=True)
class ExportMeasurement:
    band: str
    value: float
    error: float
    systematic_error: float
    upper_limit: bool
    unit: str
    bibcode: str
    note1: str
    note2: str
    source_id: str
    private: bool
    excluded: bool


@dataclass(frozen=True)
class TargetExportProjection:
    """Database-independent scientific content of one legacy rawphot file."""

    target_id: int
    sdbid: str
    main_id: str
    ra2000_deg: float
    dec2000_deg: float
    spectral_type: str | None
    spectral_type_bibcode: str | None
    parallax_mas: float | None
    parallax_error_mas: float | None
    parallax_bibcode: str | None
    primary_object_type: str | None
    measurements: tuple[ExportMeasurement, ...]


@dataclass(frozen=True)
class TargetExportSnapshot:
    """One read snapshot plus the dirty-event boundary it includes."""

    projection: TargetExportProjection
    dirty_event_watermark: int | None


def build_target_export_projection(
    target: Target,
    measurements: Iterable[NormalizedMeasurement],
    eligibility: Mapping[int, EffectiveMeasurementEligibility],
    identifiers: Iterable[ExternalIdentifier],
    simbad: SimbadMetadata | None,
) -> TargetExportProjection:
    """Build export content without database access or file-system effects."""

    ordered_measurements = list(measurements)
    band_order = {"J": 0, "H": 1, "KS": 2}
    ordered_measurements.sort(key=lambda value: band_order.get(
        value.band.replace("2MR1", "").replace("2MR2", "").replace("2M", ""),
        99,
    ))
    rows = []
    for measurement in ordered_measurements:
        eligibility_row = eligibility[measurement.id]
        note2 = measurement.note2
        suffix = _eligibility_note(eligibility_row)
        if suffix:
            note2 = f"{note2}; {suffix}" if note2 else suffix
        rows.append(ExportMeasurement(
            band=measurement.band,
            value=measurement.value,
            error=measurement.error,
            systematic_error=measurement.systematic_error,
            upper_limit=measurement.upper_limit,
            unit=measurement.unit,
            bibcode=measurement.bibcode,
            note1=measurement.note1,
            note2=note2,
            source_id=measurement.source_id,
            private=measurement.private,
            excluded=eligibility_row.excluded,
        ))

    identifier_rows = list(identifiers)
    main_id = None if simbad is None else simbad.main_id
    if main_id is None:
        main_id = next((
            item.value for item in identifier_rows
            if item.source == "simbad"
        ), None)
    if main_id is None:
        main_id = next((
            item.value for item in identifier_rows
            if item.source != "sdb"
        ), target.sdbid)
    return TargetExportProjection(
        target_id=target.id,
        sdbid=target.sdbid,
        main_id=main_id,
        ra2000_deg=target.ra2000_deg,
        dec2000_deg=target.dec2000_deg,
        spectral_type=None if simbad is None else simbad.spectral_type,
        spectral_type_bibcode=(
            None if simbad is None else simbad.spectral_type_bibcode
        ),
        parallax_mas=None if simbad is None else simbad.parallax_mas,
        parallax_error_mas=(
            None if simbad is None else simbad.parallax_error_mas
        ),
        parallax_bibcode=(
            None if simbad is None else simbad.parallax_bibcode
        ),
        primary_object_type=(
            None if simbad is None else simbad.primary_object_type
        ),
        measurements=tuple(rows),
    )


def load_target_export_snapshot(
    session: Session,
    target_reference: str | int,
) -> TargetExportSnapshot:
    """Load one consistent export read snapshot and its dirty watermark."""

    _ensure_read_snapshot(session)
    target = resolve_target(session, target_reference)
    if target is None:
        raise KeyError(f"target not found: {target_reference}")
    state = load_system_photometry_state(
        session, [target.id], expand_context=False,
    )
    identifiers = list(session.scalars(
        select(ExternalIdentifier)
        .where(ExternalIdentifier.target_id == target.id)
        .order_by(ExternalIdentifier.id)
    ))
    simbad = session.scalar(
        select(SimbadMetadata)
        .join(MetadataRun, MetadataRun.id == SimbadMetadata.run_id)
        .where(
            SimbadMetadata.target_id == target.id,
            MetadataRun.is_current.is_(True),
            MetadataRun.status == ProviderRunStatus.MATCH,
        )
    )
    projection = build_target_export_projection(
        target,
        (encounter.measurement for encounter in state.encounters),
        state.eligibility,
        identifiers,
        simbad,
    )
    return TargetExportSnapshot(
        projection=projection,
        dirty_event_watermark=export_dirty_watermark(session, target.id),
    )


def serialize_ipac(projection: TargetExportProjection) -> Table:
    """Serialize a pure target projection into the legacy Astropy table."""

    table = Table(
        rows=[(
            row.band,
            row.value,
            row.error,
            row.systematic_error,
            int(row.upper_limit),
            row.unit,
            row.bibcode,
            row.note1,
            row.note2,
            row.source_id,
            int(row.private),
            int(row.excluded),
        ) for row in projection.measurements],
        names=(
            "Band", "Phot", "Err", "Sys", "Lim", "Unit", "bibcode",
            "Note1", "Note2", "SourceID", "private", "exclude",
        ),
        dtype=(
            "U30", "f8", "f8", "f8", "i1", "U20", "U30", "U200",
            "U200", "U100", "i1", "i1",
        ),
    )
    table.meta["keywords"] = {
        "id": {"value": projection.sdbid},
        "main_id": {"value": projection.main_id},
        "raj2000": {"value": projection.ra2000_deg},
        "dej2000": {"value": projection.dec2000_deg},
        "sp_type": {"value": projection.spectral_type},
        "sp_bibcode": {"value": projection.spectral_type_bibcode},
        "plx_value": {"value": projection.parallax_mas},
        "plx_err": {"value": projection.parallax_error_mas},
        "plx_bibcode": {"value": projection.parallax_bibcode},
        "otype": {"value": projection.primary_object_type},
    }
    return table


def write_ipac_atomic(
    projection: TargetExportProjection,
    output: str | Path,
) -> Path:
    """Write and atomically replace one rawphot destination."""

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_value = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_value)
    try:
        _write_ipac_table(serialize_ipac(projection), temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def export_ipac(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    output: str | Path,
) -> Path:
    """Snapshot, atomically write, then acknowledge only included dirty events."""

    with session_factory() as session:
        snapshot = load_target_export_snapshot(session, target_reference)
    written = write_ipac_atomic(snapshot.projection, output)
    if snapshot.dirty_event_watermark is not None:
        with session_factory.begin() as session:
            mark_exported_through(
                session,
                snapshot.projection.target_id,
                snapshot.dirty_event_watermark,
            )
    return written


def _write_ipac_table(table: Table, output: Path) -> None:
    table.write(output, format="ascii.ipac", overwrite=True)


def _ensure_read_snapshot(session: Session) -> None:
    """Make SQLite establish its snapshot before the first projection read."""

    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


def _eligibility_note(
    eligibility: EffectiveMeasurementEligibility,
) -> str | None:
    if eligibility.basis == "shared_detection":
        return "Blend:shared catalog source; component export excluded"
    if eligibility.basis == "iras_alternate":
        return "IRAS duplicate:alternate PSC/FSC measurement"
    if eligibility.basis == "tdsc_preferred":
        return "Optical duplicate:TDSC component photometry preferred"
    if eligibility.action_id is not None:
        return f"Override:{eligibility.reason}"
    return None
