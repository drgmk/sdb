from __future__ import annotations

from typing import Any

import astropy.units as u
from astropy.coordinates import SkyCoord
from astroquery.simbad import Simbad
from astroquery.vizier import Vizier
from requests.adapters import HTTPAdapter

from .astrometry import angular_separation_arcsec, propagate_to_epoch
from .astroquery_config import configure_vizier_class, configured_simbad_client
from .providers import (
    Astrometry,
    Candidate,
    NameResolution,
    ProviderError,
    SimbadNeighbour,
)


class _TimeoutHTTPAdapter(HTTPAdapter):
    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        super().__init__()

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout_seconds
        return super().send(request, **kwargs)


def _set_http_timeout(client, timeout_seconds: float) -> None:
    adapter = _TimeoutHTTPAdapter(timeout_seconds)
    sessions = {client._session, client.tap._session}
    for session in sessions:
        session.mount("https://", adapter)
        session.mount("http://", adapter)


def _value(row: Any, *names: str):
    columns = {str(name).lower(): name for name in getattr(row, "colnames", ())}
    if isinstance(row, dict):
        columns.update({str(name).lower(): name for name in row})
    for name in names:
        key = columns.get(name.lower())
        if key is None:
            continue
        value = row[key]
        if getattr(value, "mask", False):
            return None
        if value is None:
            return None
        return value.item() if hasattr(value, "item") else value
    return None


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value).strip()


def _float(value) -> float | None:
    return None if value is None else float(value)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _identifier_key(value: str) -> str:
    return " ".join(str(value).upper().split())


class AstroquerySimbad:
    name = "simbad"

    def __init__(self, timeout_seconds: float = 30.0):
        self.client = configured_simbad_client(Simbad())
        _set_http_timeout(self.client, timeout_seconds)

    def resolve_name(self, name: str) -> NameResolution | None:
        literal = "'" + name.replace("'", "''") + "'"
        query = f"""
            SELECT TOP 2 b.main_id, b.ra, b.dec, b.pmra, b.pmdec,
                   b.plx_value, b.rvz_radvel, b.oid,
                   b.coo_bibcode, b.pm_bibcode, b.plx_bibcode,
                   b.rvz_bibcode
            FROM basic AS b JOIN ident AS i ON i.oidref=b.oid
            WHERE i.id={literal}
        """
        try:
            table = self.client.query_tap(query)
        except Exception as error:  # astroquery exposes several transport exception types
            raise ProviderError(f"SIMBAD query failed: {error}", transient=True) from error
        if table is None or len(table) == 0:
            return None
        if len(table) > 1:
            raise ProviderError(f"SIMBAD identifier is ambiguous: {name}")
        parsed = self.parse_row(table[0])
        oid = int(_value(table[0], "oid"))
        try:
            identifiers = self.client.query_tap(
                f"SELECT id FROM ident WHERE oidref={oid}"
            )
        except Exception as error:
            raise ProviderError(f"SIMBAD identifier query failed: {error}", transient=True) from error
        values = tuple(
            value
            for value in (_text(_value(row, "id")) for row in identifiers)
            if value
        )
        return NameResolution(parsed.main_id, parsed.astrometry, values)

    def search_region(
        self,
        astrometry: Astrometry,
        *,
        radius_arcsec: float,
        limit: int = 100,
    ) -> list[SimbadNeighbour]:
        if radius_arcsec <= 0:
            raise ValueError("SIMBAD search radius must be positive")
        if limit < 1:
            raise ValueError("SIMBAD search limit must be positive")
        radius_deg = radius_arcsec / 3600.0
        row_limit = min(int(limit), 500)
        query = f"""
            SELECT TOP {row_limit}
                   b.oid, b.main_id, b.ra, b.dec, b.pmra, b.pmdec,
                   b.plx_value, b.rvz_radvel, b.otype, a.otypes,
                   b.sp_type, b.coo_bibcode, b.pm_bibcode,
                   b.plx_bibcode, b.rvz_bibcode, ot.label AS otype_label,
                   ot.description AS otype_description,
                   DISTANCE(
                       POINT('ICRS', b.ra, b.dec),
                       POINT('ICRS', {astrometry.ra_deg}, {astrometry.dec_deg})
                   ) AS separation_deg
            FROM basic AS b
            LEFT JOIN alltypes AS a ON a.oidref=b.oid
            LEFT JOIN otypedef AS ot ON ot.otype=b.otype
            WHERE 1=CONTAINS(
                POINT('ICRS', b.ra, b.dec),
                CIRCLE(
                    'ICRS', {astrometry.ra_deg}, {astrometry.dec_deg},
                    {radius_deg}
                )
            )
            ORDER BY separation_deg
        """
        try:
            table = self.client.query_tap(query)
        except Exception as error:
            raise ProviderError(
                f"SIMBAD region query failed: {error}", transient=True
            ) from error
        result = []
        for row in (() if table is None else table):
            parsed = self.parse_row(row)
            oid = _value(row, "oid")
            if oid is None:
                raise ProviderError("SIMBAD region response omitted oid")
            raw_types = _text(_value(row, "otypes")) or ""
            separation_deg = _float(_value(row, "separation_deg"))
            separation = (
                angular_separation_arcsec(astrometry, parsed.astrometry)
                if separation_deg is None
                else separation_deg * 3600.0
            )
            result.append(SimbadNeighbour(
                oid=int(oid),
                main_id=parsed.main_id,
                astrometry=parsed.astrometry,
                separation_arcsec=separation,
                primary_object_type=_text(_value(row, "otype")),
                object_type_label=_text(_value(row, "otype_label")),
                object_type_description=_text(
                    _value(row, "otype_description")
                ),
                object_types=tuple(
                    value for value in raw_types.split("|") if value
                ),
                spectral_type=_text(_value(row, "sp_type")),
            ))
        return sorted(
            result,
            key=lambda value: (value.separation_arcsec, value.main_id),
        )

    def resolve_many(self, names: tuple[str, ...]) -> dict[str, NameResolution | None]:
        unique_names = tuple(dict.fromkeys(name for name in names if name))
        if not unique_names:
            return {}
        literals = ", ".join(_literal(name) for name in unique_names)
        query = f"""
            SELECT i.id AS input_id, b.main_id, b.ra, b.dec, b.pmra, b.pmdec,
                   b.plx_value, b.rvz_radvel, b.oid,
                   b.coo_bibcode, b.pm_bibcode, b.plx_bibcode,
                   b.rvz_bibcode
            FROM ident AS i
            JOIN basic AS b ON i.oidref=b.oid
            WHERE i.id IN ({literals})
        """
        try:
            table = self.client.query_tap(query)
        except Exception as error:
            raise ProviderError(f"SIMBAD bulk query failed: {error}", transient=True) from error
        names_by_key: dict[str, list[str]] = {}
        for name in unique_names:
            names_by_key.setdefault(_identifier_key(name), []).append(name)
        rows_by_name: dict[str, list[Any]] = {name: [] for name in unique_names}
        if table is not None:
            for row in table:
                input_id = _text(_value(row, "input_id"))
                if input_id is None:
                    continue
                for name in names_by_key.get(_identifier_key(input_id), ()):
                    rows_by_name[name].append(row)

        oid_to_names: dict[int, list[str]] = {}
        result: dict[str, NameResolution | None] = {}
        for name, rows in rows_by_name.items():
            if not rows:
                result[name] = None
                continue
            oids = {
                int(value)
                for value in (_value(row, "oid") for row in rows)
                if value is not None
            }
            if len(oids) > 1:
                raise ProviderError(f"SIMBAD identifier is ambiguous: {name}")
            oid = next(iter(oids))
            oid_to_names.setdefault(oid, []).append(name)

        identifiers_by_oid = self._identifiers_for_oids(tuple(oid_to_names))
        for oid, matched_names in oid_to_names.items():
            # Any row for this oid has the same object-level astrometry.
            row = next(
                row for rows in rows_by_name.values() for row in rows
                if _value(row, "oid") is not None and int(_value(row, "oid")) == oid
            )
            parsed = self.parse_row(row)
            value = NameResolution(
                parsed.main_id,
                parsed.astrometry,
                identifiers_by_oid.get(oid, ()),
            )
            for name in matched_names:
                result[name] = value
        return result

    def _identifiers_for_oids(self, oids: tuple[int, ...]) -> dict[int, tuple[str, ...]]:
        if not oids:
            return {}
        literals = ", ".join(str(oid) for oid in oids)
        try:
            rows = self.client.query_tap(
                f"SELECT oidref, id FROM ident WHERE oidref IN ({literals})"
            )
        except Exception as error:
            raise ProviderError(
                f"SIMBAD bulk identifier query failed: {error}", transient=True
            ) from error
        values: dict[int, list[str]] = {oid: [] for oid in oids}
        if rows is None:
            return {oid: () for oid in oids}
        for row in rows:
            oid = _value(row, "oidref")
            identifier = _text(_value(row, "id"))
            if oid is not None and identifier:
                values.setdefault(int(oid), []).append(identifier)
        return {oid: tuple(items) for oid, items in values.items()}

    @staticmethod
    def parse_row(row) -> NameResolution:
        main_id = _text(_value(row, "main_id"))
        ra = _float(_value(row, "ra"))
        dec = _float(_value(row, "dec"))
        if not main_id or ra is None or dec is None:
            raise ProviderError("SIMBAD response omitted main_id or coordinates")
        raw_ids = _text(_value(row, "ids")) or ""
        identifiers = tuple(value.strip() for value in raw_ids.split("|") if value.strip())
        value = Astrometry(
            ra_deg=ra,
            dec_deg=dec,
            epoch=2000.0,
            pm_ra_cosdec_masyr=_float(_value(row, "pmra")),
            pm_dec_masyr=_float(_value(row, "pmdec")),
            parallax_mas=_float(_value(row, "plx_value", "plx")),
            radial_velocity_kms=_float(_value(row, "rvz_radvel")),
            source="simbad",
            source_id=main_id,
            position_bibcode=_text(_value(row, "coo_bibcode")),
            proper_motion_bibcode=_text(_value(row, "pm_bibcode")),
            parallax_bibcode=_text(_value(row, "plx_bibcode")),
            radial_velocity_bibcode=_text(_value(row, "rvz_bibcode")),
        )
        return NameResolution(main_id=main_id, astrometry=value, identifiers=identifiers)


class AstroqueryGaia:
    name = "gaia_dr3"

    catalog = "I/355/gaiadr3"

    def __init__(self, radius_arcsec: float = 10.0, timeout_seconds: float = 30.0):
        self.radius_arcsec = radius_arcsec
        # Use the Gaia DR3 VizieR mirror: it is the same astrometric release,
        # supports bounded HTTP timeouts, and avoids long-lived TAP jobs.
        configure_vizier_class(Vizier)
        self.client = Vizier(
            columns=["Source", "RA_ICRS", "DE_ICRS", "Plx", "pmRA", "pmDE", "RV", "+_r"],
            row_limit=10,
        )
        self.client.TIMEOUT = timeout_seconds

    def search(self, astrometry: Astrometry) -> list[Candidate]:
        query_position = propagate_to_epoch(astrometry, 2016.0)
        coordinate = SkyCoord(query_position.ra_deg * u.deg, query_position.dec_deg * u.deg, frame="icrs")
        try:
            tables = self.client.query_region(
                coordinate,
                radius=self.radius_arcsec * u.arcsec,
                catalog=self.catalog,
            )
        except Exception as error:
            raise ProviderError(f"Gaia DR3 query failed: {error}", transient=True) from error
        if not tables:
            return []
        return [self.parse_row(row) for row in tables[0]]

    @staticmethod
    def parse_row(row) -> Candidate:
        source_id = _text(_value(row, "source_id", "Source"))
        ra = _float(_value(row, "ra", "RA_ICRS"))
        dec = _float(_value(row, "dec", "DE_ICRS"))
        if not source_id or ra is None or dec is None:
            raise ProviderError("Gaia DR3 response omitted source_id or coordinates")
        pm_ra = _float(_value(row, "pmra", "pmRA"))
        pm_dec = _float(_value(row, "pmdec", "pmDE"))
        parallax = _float(_value(row, "parallax", "Plx"))
        radial_velocity = _float(_value(row, "radial_velocity", "RV"))
        gaia_dr3_bibcode = "2023A&A...674A...1G"
        value = Astrometry(
            ra_deg=ra,
            dec_deg=dec,
            epoch=_float(_value(row, "ref_epoch")) or 2016.0,
            pm_ra_cosdec_masyr=pm_ra,
            pm_dec_masyr=pm_dec,
            parallax_mas=parallax,
            radial_velocity_kms=radial_velocity,
            source="gaia_dr3",
            source_id=source_id,
            position_bibcode=gaia_dr3_bibcode,
            proper_motion_bibcode=(
                gaia_dr3_bibcode if pm_ra is not None and pm_dec is not None else None
            ),
            parallax_bibcode=gaia_dr3_bibcode if parallax is not None else None,
            radial_velocity_bibcode=gaia_dr3_bibcode if radial_velocity is not None else None,
        )
        return Candidate(source_id=source_id, astrometry=value)
