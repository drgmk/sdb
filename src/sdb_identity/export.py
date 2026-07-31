from __future__ import annotations

from pathlib import Path

from astropy.table import Table

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    ExternalIdentifier,
    MetadataRun,
    SimbadMetadata,
    Target,
)
from .catalog_measurements import current_measurement_encounters
from .dirty import clear_export_dirty
from .measurement_eligibility import effective_measurement_eligibility
from .targets import resolve_target
from .vocabulary import ProviderRunStatus


def export_ipac(
    session_factory: sessionmaker[Session],
    target_reference: str | int,
    output: str | Path,
) -> Path:
    output = Path(output)
    with session_factory() as session:
        target = resolve_target(session, target_reference)
        if target is None:
            raise KeyError(f"target not found: {target_reference}")
        encounters = current_measurement_encounters(session, [target.id])
        measurements = [row.measurement for row in encounters]
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
                MetadataRun.status == ProviderRunStatus.MATCH,
            )
        )
        eligibility = effective_measurement_eligibility(
            session, [value.id for value in measurements],
        )

    band_order = {"J": 0, "H": 1, "KS": 2}
    measurements.sort(key=lambda value: band_order.get(value.band.replace("2MR1", "").replace("2MR2", "").replace("2M", ""), 99))
    rows = []
    for value in measurements:
        eligibility_row = eligibility[value.id]
        excluded = eligibility_row.excluded
        note2 = value.note2
        if eligibility_row.basis == "shared_detection":
            suffix = "Blend:shared catalog source; component export excluded"
            note2 = f"{note2}; {suffix}" if note2 else suffix
        if eligibility_row.basis == "iras_alternate":
            suffix = "IRAS duplicate:alternate PSC/FSC measurement"
            note2 = f"{note2}; {suffix}" if note2 else suffix
        if eligibility_row.basis == "tdsc_preferred":
            suffix = "Optical duplicate:TDSC component photometry preferred"
            note2 = f"{note2}; {suffix}" if note2 else suffix
        if eligibility_row.action_id is not None:
            suffix = f"Override:{eligibility_row.reason}"
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
