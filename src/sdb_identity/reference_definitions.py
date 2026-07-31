from __future__ import annotations

from dataclasses import dataclass
import math
import re

import astropy.units as u
from astropy.coordinates import SkyCoord

from .identifiers import normalize_identifier
from .serialization import row_float, row_payload, row_text
from .ubv_components import ubv_component_identifiers
from .tdsc_components import tdsc_component_identifiers
from .v70a_components import v70a_component_identifiers


GASPAR_CATALOG = "J/ApJ/768/25"
GASPAR_MAIN_TABLE = f"{GASPAR_CATALOG}/table2"
GASPAR_REFS_TABLE = f"{GASPAR_CATALOG}/refs"
GASPAR_BIBCODE = "2013ApJ...768...25G"
V70A_CATALOG = "V/70A"
V70A_MAIN_TABLE = f"{V70A_CATALOG}/catalog"
V70A_BIBCODE = "1995yCat.5070....0G"
IRAS_PSC_CATALOG = "II/125"
IRAS_PSC_MAIN_TABLE = f"{IRAS_PSC_CATALOG}/main"
IRAS_PSC_BIBCODE = "1988IRASP.C......0J"
IRAS_FSC_CATALOG = "II/156A"
IRAS_FSC_MAIN_TABLE = f"{IRAS_FSC_CATALOG}/main"
IRAS_FSC_BIBCODE = "1990IRASF.C......0M"
HIP2_CATALOG = "I/311"
HIP2_MAIN_TABLE = f"{HIP2_CATALOG}/hip2"
HIP2_BIBCODE = "2007A&A...474..653V"
TDSC_CATALOG = "I/276"
TDSC_MAIN_TABLE = f"{TDSC_CATALOG}/catalog"
TDSC_SUPPLEMENT_TABLE = f"{TDSC_CATALOG}/supplem"
TDSC_BIBCODE = "2002A&A...384..180F"
UBVMEANS_CATALOG = "II/168"
UBVMEANS_MAIN_TABLE = f"{UBVMEANS_CATALOG}/ubvmeans"
UBVMEANS_BIBCODE = "2006yCat.2168....0M"
PAUNZEN15_CATALOG = "J/A+A/580/A23"
PAUNZEN15_MAIN_TABLE = f"{PAUNZEN15_CATALOG}/catalog"
PAUNZEN15_BIBCODE = "2015A&A...580A..23P"
KOEN10_CATALOG = "J/MNRAS/403/1949"
KOEN10_MAIN_TABLE = f"{KOEN10_CATALOG}/ubvri"
KOEN10_JHKL_TABLE = f"{KOEN10_CATALOG}/jhkl"
KOEN10_BIBCODE = "2010MNRAS.403.1949K"


@dataclass(frozen=True)
class RelationshipDefinition:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    parser: str
    description: str


@dataclass(frozen=True)
class IdentifierAuditPolicy:
    simbad_patterns: tuple[str, ...]
    description: str

    def relevant(self, value: str) -> bool:
        return any(re.match(pattern, value, re.IGNORECASE) for pattern in self.simbad_patterns)


@dataclass(frozen=True)
class SnapshotCatalogDefinition:
    adapter: str
    catalog: str
    main_table: str
    primary_identifier: str
    key_columns: tuple[str, ...]
    identifier_columns: tuple[tuple[str, str | None], ...]
    ra_column: str
    dec_column: str
    composite_identifier_columns: tuple[
        tuple[str, tuple[str, ...], str], ...
    ] = ()
    coordinate_format: str = "degrees"
    coordinate_frame: str = "icrs"
    coordinate_equinox: str | None = None
    query_epoch: float = 2000.0
    radius_arcsec: float = 5.0
    bibliography: str = ""
    band_wavelengths_micron: tuple[tuple[str, float], ...] = ()
    match_tables: tuple[str, ...] = ()
    relationships: tuple[RelationshipDefinition, ...] = ()
    identifier_audit: IdentifierAuditPolicy | None = None

    @property
    def tables_for_matching(self) -> tuple[str, ...]:
        return self.match_tables or (self.main_table,)

    def _base_identifiers(self, payload: dict[str, object]) -> tuple[str, ...]:
        result = []
        for column, prefix in self.identifier_columns:
            value = row_text(payload, column)
            if not value:
                continue
            result.append(f"{prefix} {value}" if prefix else value)
        for prefix, columns, separator in self.composite_identifier_columns:
            values = [row_text(payload, column) for column in columns]
            if all(values):
                result.append(f"{prefix} {separator.join(values)}")
        if self.adapter == "v70a":
            name = row_text(payload, "Name") or ""
            match = re.fullmatch(r"(?:NN|Wo)\s*0*(\d+)(.*)", name, re.IGNORECASE)
            if match:
                suffix = match.group(2).strip()
                result.append(
                    f"GJ {int(match.group(1))}{f' {suffix}' if suffix else ''}"
                )
        return tuple(result)

    def identifiers(self, payload: dict[str, object]) -> tuple[str, ...]:
        result = self._base_identifiers(payload)
        if self.adapter == "v70a":
            return v70a_component_identifiers(result, payload)
        if self.adapter == "ubvmeans":
            return ubv_component_identifiers(result, payload)
        if self.adapter == "tdsc":
            return tdsc_component_identifiers(result, payload)
        return result

    def lookup_identifiers(self, payload: dict[str, object]) -> tuple[str, ...]:
        """Return broad row-discovery aliases without making them identity."""
        base = self._base_identifiers(payload)
        specific = self.identifiers(payload)
        return tuple(dict.fromkeys((*specific, *base)))

    def row_key(self, payload: dict[str, object], fallback: str) -> str:
        values = [(column, row_text(payload, column)) for column in self.key_columns]
        if not values or not values[0][1]:
            return fallback
        key = values[0][1]
        for column, value in values[1:]:
            if value:
                key += f"|{column}={value}"
        return key

    def position(self, payload: dict[str, object]) -> tuple[float | None, float | None]:
        if self.coordinate_format == "degrees":
            return row_float(payload, self.ra_column), row_float(payload, self.dec_column)
        if self.coordinate_format == "radians":
            ra = row_float(payload, self.ra_column)
            dec = row_float(payload, self.dec_column)
            if ra is None or dec is None:
                return None, None
            return float((ra * u.rad).to_value(u.deg)) % 360.0, float(
                (dec * u.rad).to_value(u.deg)
            )
        if self.coordinate_format == "tdsc_j2000":
            ra = row_float(payload, "RAJ2000", "RAdeg")
            dec = row_float(payload, "DEJ2000", "DEdeg")
            if ra is None or dec is None:
                return None, None
            if row_float(payload, "RAJ2000") is not None:
                return ra % 360.0, dec
            pm_ra = row_float(payload, "pmRA")
            pm_dec = row_float(payload, "pmDE")
            epoch_ra = row_float(payload, "EpRA")
            epoch_dec = row_float(payload, "EpDE")
            if pm_ra is not None and epoch_ra is not None:
                cos_dec = math.cos(math.radians(dec))
                if abs(cos_dec) > 1e-12:
                    ra += (2000.0 - epoch_ra) * pm_ra / 3_600_000.0 / cos_dec
            if pm_dec is not None and epoch_dec is not None:
                dec += (2000.0 - epoch_dec) * pm_dec / 3_600_000.0
            return ra % 360.0, dec
        if self.coordinate_format == "iras_b1950_components":
            values = [
                row_float(payload, column)
                for column in ("RAh", "RAm", "RAds", "DEd", "DEm", "DEs")
            ]
            if any(value is None for value in values):
                return None, None
            rah, ram, rads, ded, dem, des = values
            ra_deg = 15.0 * (rah + ram / 60.0 + (rads / 10.0) / 3600.0)
            dec_deg = ded + dem / 60.0 + des / 3600.0
            if row_text(payload, "DE-") == "-":
                dec_deg = -dec_deg
            coordinate = SkyCoord(
                ra_deg * u.deg,
                dec_deg * u.deg,
                frame="fk4",
                equinox="B1950",
            ).icrs
            return float(coordinate.ra.deg), float(coordinate.dec.deg)
        ra = row_text(payload, self.ra_column)
        dec = row_text(payload, self.dec_column)
        if not ra or not dec:
            return None, None
        kwargs = {"frame": self.coordinate_frame}
        if self.coordinate_equinox is not None:
            kwargs["equinox"] = self.coordinate_equinox
        coordinate = SkyCoord(
            ra, dec, unit=(u.hourangle, u.deg), **kwargs
        ).icrs
        return float(coordinate.ra.deg), float(coordinate.dec.deg)


GASPAR_DEFINITION = SnapshotCatalogDefinition(
    adapter="gaspar13",
    catalog=GASPAR_CATALOG,
    main_table=GASPAR_MAIN_TABLE,
    primary_identifier="Name",
    key_columns=("Name",),
    identifier_columns=(("Name", None),),
    ra_column="_RA",
    dec_column="_DE",
    bibliography=GASPAR_BIBCODE,
    band_wavelengths_micron=(("MIPS70", 71.4193),),
    relationships=(RelationshipDefinition(
        GASPAR_MAIN_TABLE,
        "r_Age",
        GASPAR_REFS_TABLE,
        "Ref",
        "comma_separated_ints",
        "Age reference codes in table2 resolve to bibliography rows in refs.",
    ),),
)

V70A_DEFINITION = SnapshotCatalogDefinition(
    adapter="v70a",
    catalog=V70A_CATALOG,
    main_table=V70A_MAIN_TABLE,
    primary_identifier="Name",
    key_columns=("Name", "Comp"),
    identifier_columns=(
        ("Name", None), ("HD", "HD"), ("DM", None), ("Giclas", None),
        ("LHS", "LHS"), ("OtherName", None),
    ),
    ra_column="_RA.icrs",
    dec_column="_DE.icrs",
    coordinate_format="sexagesimal",
    bibliography=V70A_BIBCODE,
)

IRAS_PSC_DEFINITION = SnapshotCatalogDefinition(
    adapter="iras_psc",
    catalog=IRAS_PSC_CATALOG,
    main_table=IRAS_PSC_MAIN_TABLE,
    primary_identifier="IRAS",
    key_columns=("IRAS",),
    identifier_columns=(("IRAS", "IRAS"),),
    ra_column="RA1950",
    dec_column="DE1950",
    coordinate_format="iras_b1950_components",
    coordinate_frame="fk4",
    coordinate_equinox="B1950",
    query_epoch=1983.5,
    radius_arcsec=60.0,
    bibliography=IRAS_PSC_BIBCODE,
    band_wavelengths_micron=(
        ("IRAS12", 11.2248),
        ("IRAS25", 23.3438),
        ("IRAS60", 59.3524),
        ("IRAS100", 100.3468),
    ),
    identifier_audit=IdentifierAuditPolicy(
        simbad_patterns=(r"^IRAS\s+\d",),
        description="SIMBAD IRAS identifiers without the FSC F prefix",
    ),
)

IRAS_FSC_DEFINITION = SnapshotCatalogDefinition(
    adapter="iras_fsc",
    catalog=IRAS_FSC_CATALOG,
    main_table=IRAS_FSC_MAIN_TABLE,
    primary_identifier="IRAS",
    key_columns=("IRAS",),
    identifier_columns=(("IRAS", "IRAS"),),
    ra_column="RA1950",
    dec_column="DE1950",
    coordinate_format="iras_b1950_components",
    coordinate_frame="fk4",
    coordinate_equinox="B1950",
    query_epoch=1983.5,
    radius_arcsec=60.0,
    bibliography=IRAS_FSC_BIBCODE,
    band_wavelengths_micron=(
        ("IRAS12", 11.2248),
        ("IRAS25", 23.3438),
        ("IRAS60", 59.3524),
        ("IRAS100", 100.3468),
    ),
    identifier_audit=IdentifierAuditPolicy(
        simbad_patterns=(r"^IRAS\s+F",),
        description="SIMBAD IRAS Faint Source Catalog identifiers",
    ),
)

HIP2_DEFINITION = SnapshotCatalogDefinition(
    adapter="hip2",
    catalog=HIP2_CATALOG,
    main_table=HIP2_MAIN_TABLE,
    primary_identifier="HIP",
    key_columns=("HIP",),
    identifier_columns=(("HIP", "HIP"),),
    ra_column="RArad",
    dec_column="DErad",
    coordinate_format="radians",
    query_epoch=1991.25,
    radius_arcsec=2.0,
    bibliography=HIP2_BIBCODE,
    band_wavelengths_micron=(("HP", 0.5420),),
)

TDSC_DEFINITION = SnapshotCatalogDefinition(
    adapter="tdsc",
    catalog=TDSC_CATALOG,
    main_table=TDSC_MAIN_TABLE,
    match_tables=(TDSC_MAIN_TABLE, TDSC_SUPPLEMENT_TABLE),
    primary_identifier="TDSC",
    key_columns=("TDSC", "m_TDSC"),
    identifier_columns=(
        ("HIP", "HIP"), ("HD", "HD"), ("WDS", "WDS"),
    ),
    composite_identifier_columns=(("TYC", ("TYC1", "TYC2", "TYC3"), "-"),),
    ra_column="RAJ2000",
    dec_column="DEJ2000",
    coordinate_format="tdsc_j2000",
    query_epoch=2000.0,
    radius_arcsec=2.0,
    bibliography=TDSC_BIBCODE,
    band_wavelengths_micron=(("BT", 0.4203), ("VT", 0.5317)),
)

UBVMEANS_DEFINITION = SnapshotCatalogDefinition(
    adapter="ubvmeans",
    catalog=UBVMEANS_CATALOG,
    main_table=UBVMEANS_MAIN_TABLE,
    primary_identifier="SimbadName",
    key_columns=("LID", "m_LID"),
    identifier_columns=(("SimbadName", None),),
    ra_column="_RA",
    dec_column="_DE",
    query_epoch=2000.0,
    radius_arcsec=2.0,
    bibliography=UBVMEANS_BIBCODE,
    # Colour indices are placed at the shortest contributing passband so
    # catalogue rows retain the natural U-to-V ordering in review.
    band_wavelengths_micron=(
        ("UJ_BJ", 0.36),
        ("BJ_VJ", 0.44),
        ("VJ", 0.5498),
    ),
)

PAUNZEN15_DEFINITION = SnapshotCatalogDefinition(
    adapter="paunzen15",
    catalog=PAUNZEN15_CATALOG,
    main_table=PAUNZEN15_MAIN_TABLE,
    primary_identifier="TYC",
    key_columns=("TYC1", "TYC2", "TYC3"),
    identifier_columns=(),
    composite_identifier_columns=(("TYC", ("TYC1", "TYC2", "TYC3"), "-"),),
    ra_column="RAICRS",
    dec_column="DEICRS",
    query_epoch=2000.0,
    radius_arcsec=2.0,
    bibliography=PAUNZEN15_BIBCODE,
    # Stroemgren indices span several filters; use the shortest contributing
    # passband as their review-order wavelength.
    band_wavelengths_micron=(
        ("STROMC1", 0.35),
        ("STROMM1", 0.41),
        ("BS_YS", 0.467),
    ),
)

KOEN10_DEFINITION = SnapshotCatalogDefinition(
    adapter="koen10",
    catalog=KOEN10_CATALOG,
    main_table=KOEN10_MAIN_TABLE,
    primary_identifier="HIP",
    key_columns=("HIP",),
    identifier_columns=(("HIP", "HIP"),),
    ra_column="_RA",
    dec_column="_DE",
    query_epoch=2000.0,
    radius_arcsec=2.0,
    bibliography=KOEN10_BIBCODE,
    band_wavelengths_micron=(
        ("UJ_BJ", 0.36),
        ("BJ_VJ", 0.44),
        ("VJ", 0.5498),
        ("VJ_RC", 0.5498),
        ("VJ_IC", 0.5498),
    ),
)

SNAPSHOT_CATALOGS = {
    definition.adapter: definition
    for definition in (
        GASPAR_DEFINITION, V70A_DEFINITION,
        IRAS_PSC_DEFINITION, IRAS_FSC_DEFINITION,
        HIP2_DEFINITION, TDSC_DEFINITION,
        UBVMEANS_DEFINITION, PAUNZEN15_DEFINITION, KOEN10_DEFINITION,
    )
}
