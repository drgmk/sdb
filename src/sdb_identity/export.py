from __future__ import annotations

from pathlib import Path

from astropy.table import Table

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    CatalogRun,
    CuratedPhotometryOverride,
    ExternalIdentifier,
    IrasBandSelection,
    IrasDetectionFamily,
    MetadataRun,
    NormalizedMeasurement,
    PhotometryOverride,
    RawCatalogRow,
    SimbadMetadata,
    Target,
)
from .catalog_measurements import current_measurement_encounters
from .service import normalize_identifier
from .dirty import clear_export_dirty


def _target(session: Session, reference: str | int) -> Target | None:
    if isinstance(reference, int) or str(reference).isdigit():
        return session.get(Target, int(reference))
    target = session.scalar(select(Target).where(Target.sdbid == str(reference)))
    if target:
        return target
    identifier = session.scalar(
        select(ExternalIdentifier).where(
            ExternalIdentifier.normalized_value == normalize_identifier(str(reference))
        ).limit(1)
    )
    return None if identifier is None else session.get(Target, identifier.target_id)


def export_ipac(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    output: str | Path,
) -> Path:
    output = Path(output)
    with session_factory() as session:
        target = _target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        encounters = current_measurement_encounters(session, [target.id])
        measurements = [row.measurement for row in encounters]
        encounter_raw_by_measurement = {
            row.measurement.id: row.raw_row for row in encounters
        }
        identifiers = list(
            session.scalars(
                select(ExternalIdentifier)
                .where(ExternalIdentifier.target_id == target.id)
                .order_by(ExternalIdentifier.id)
            )
        )
        simbad = session.scalar(
            select(SimbadMetadata)
            .join(MetadataRun, MetadataRun.id == SimbadMetadata.run_id)
            .where(
                SimbadMetadata.target_id == target.id,
                MetadataRun.is_current.is_(True),
                MetadataRun.status == "match",
            )
        )
        overrides = list(
            session.scalars(
                select(PhotometryOverride)
                .where(PhotometryOverride.target_id == target.id)
                .order_by(PhotometryOverride.id)
            )
        )
        curated_overrides = list(session.scalars(
            select(CuratedPhotometryOverride).order_by(CuratedPhotometryOverride.id)
        ))
        shared_source_keys = {
            (provider, source_id)
            for provider, source_id, target_count in session.execute(
                select(
                    NormalizedMeasurement.provider,
                    NormalizedMeasurement.source_id,
                    func.count(func.distinct(CatalogRun.target_id)),
                )
                .join(
                    RawCatalogRow,
                    RawCatalogRow.detection_id == NormalizedMeasurement.detection_id,
                )
                .join(CatalogRun, CatalogRun.id == RawCatalogRow.run_id)
                .where(
                    CatalogRun.is_current.is_(True),
                    CatalogRun.status == "match",
                    RawCatalogRow.accepted.is_(True),
                )
                .group_by(NormalizedMeasurement.provider, NormalizedMeasurement.source_id)
            )
            if target_count > 1
        }
        iras_alternate_ids = set(session.scalars(
            select(IrasBandSelection.alternate_measurement_id)
            .join(
                IrasDetectionFamily,
                IrasDetectionFamily.id == IrasBandSelection.family_id,
            )
            .where(
                IrasDetectionFamily.target_id == target.id,
                IrasDetectionFamily.is_current.is_(True),
                IrasDetectionFamily.status == "associated",
            )
        ))

    band_order = {"J": 0, "H": 1, "KS": 2}
    measurements.sort(key=lambda value: band_order.get(value.band.replace("2MR1", "").replace("2MR2", "").replace("2M", ""), 99))
    latest_overrides = {
        (value.provider, value.band): value
        for value in overrides
    }
    latest_curated_overrides = {
        (value.dataset, value.record_no): value
        for value in curated_overrides
    }
    tdsc_preferred_bands = set()
    for value in measurements:
        if value.provider != "tdsc":
            continue
        override = latest_overrides.get((value.provider, value.band))
        excluded = value.excluded if override is None else override.excluded
        if not excluded:
            tdsc_preferred_bands.add(value.band)
    rows = []
    for value in measurements:
        override = latest_overrides.get((value.provider.lower(), value.band.upper()))
        raw = encounter_raw_by_measurement.get(value.id)
        curated_override = None
        if raw is not None and raw.source_id.startswith(f"{value.provider}:"):
            try:
                record_no = int(raw.source_id.rsplit(":", 1)[1])
            except ValueError:
                record_no = None
            if record_no is not None:
                curated_override = latest_curated_overrides.get((value.provider, record_no))
        effective_override = curated_override or override
        shared_source = (value.provider, value.source_id) in shared_source_keys
        excluded = value.excluded if effective_override is None else effective_override.excluded
        if shared_source and effective_override is None:
            excluded = True
        iras_alternate = value.id in iras_alternate_ids
        if iras_alternate and effective_override is None:
            excluded = True
        optical_alternate = (
            value.provider == "tycho2" and value.band in tdsc_preferred_bands
        )
        if optical_alternate and effective_override is None:
            excluded = True
        note2 = value.note2
        if shared_source:
            suffix = "Blend:shared catalog source; component export excluded"
            note2 = f"{note2}; {suffix}" if note2 else suffix
        if iras_alternate:
            suffix = "IRAS duplicate:alternate PSC/FSC measurement"
            note2 = f"{note2}; {suffix}" if note2 else suffix
        if optical_alternate:
            suffix = "Optical duplicate:TDSC component photometry preferred"
            note2 = f"{note2}; {suffix}" if note2 else suffix
        if effective_override is not None:
            suffix = f"Override:{effective_override.reason}"
            note2 = f"{note2}; {suffix}" if note2 else suffix
        rows.append(
            (
                value.band,
                value.value,
                value.error,
                value.systematic_error,
                int(value.upper_limit),
                value.unit,
                value.bibcode,
                value.note1,
                note2,
                value.source_id,
                int(value.private),
                int(excluded),
            )
        )
    table = Table(
        rows=rows,
        names=(
            "Band", "Phot", "Err", "Sys", "Lim", "Unit", "bibcode",
            "Note1", "Note2", "SourceID", "private", "exclude",
        ),
        dtype=("U30", "f8", "f8", "f8", "i1", "U20", "U30", "U200", "U200", "U100", "i1", "i1"),
    )
    main_id = simbad.main_id if simbad is not None else None
    if main_id is None:
        main_id = next((item.value for item in identifiers if item.source == "simbad"), None)
    if main_id is None:
        main_id = next((item.value for item in identifiers if item.source != "sdb"), target.sdbid)
    table.meta["keywords"] = {
        "id": {"value": target.sdbid},
        "main_id": {"value": main_id},
        "raj2000": {"value": target.ra2000_deg},
        "dej2000": {"value": target.dec2000_deg},
        "sp_type": {"value": None if simbad is None else simbad.spectral_type},
        "sp_bibcode": {"value": None if simbad is None else simbad.spectral_type_bibcode},
        "plx_value": {"value": None if simbad is None else simbad.parallax_mas},
        "plx_err": {"value": None if simbad is None else simbad.parallax_error_mas},
        "plx_bibcode": {"value": None if simbad is None else simbad.parallax_bibcode},
        "otype": {"value": None if simbad is None else simbad.primary_object_type},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    table.write(output, format="ascii.ipac", overwrite=True)
    with session_factory() as session, session.begin():
        clear_export_dirty(session, target.id)
    return output
