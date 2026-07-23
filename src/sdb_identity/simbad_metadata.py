from __future__ import annotations

from typing import Any

import astropy.units as u
from astropy.coordinates import SkyCoord
from astroquery.simbad import Simbad

from .astroquery_config import configured_simbad_client
from .metadata import (
    MetadataQueryContext,
    MetadataQueryResult,
    ObjectTypeValue,
    RelationshipValue,
    SimbadSnapshot,
)
from .providers import ProviderError
from .live_providers import _set_http_timeout


def _value(row: Any, *names: str):
    columns = {str(name).lower(): name for name in getattr(row, "colnames", ())}
    if isinstance(row, dict):
        columns.update({str(name).lower(): name for name in row})
    for name in names:
        key = columns.get(name.lower())
        if key is None:
            continue
        value = row[key]
        if getattr(value, "mask", False) or value is None:
            return None
        return value.item() if hasattr(value, "item") else value
    return None


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return str(value).strip()


def _float(value) -> float | None:
    return None if value is None else float(value)


def _int(value) -> int | None:
    return None if value is None else int(value)


def _json_row(row) -> dict[str, object]:
    if isinstance(row, dict):
        names = row.keys()
    else:
        names = row.colnames
    result = {}
    for name in names:
        value = _value(row, str(name))
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            value = str(value)
        result[str(name)] = value
    return result


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _identifier_key(value: str) -> str:
    return "".join(str(value).casefold().split())


class AstroquerySimbadMetadata:
    name = "simbad"
    release = "SIMBAD TAP"
    radius_arcsec = 2.0

    CORE_COLUMNS = """
        b.oid, b.main_id, b.ra, b.dec, b.sp_type, b.sp_bibcode,
        b.plx_value, b.plx_err, b.plx_bibcode,
        b.pmra, b.pmdec, b.pm_bibcode,
        b.rvz_radvel, b.rvz_err, b.rvz_bibcode,
        b.otype, a.otypes
    """

    def __init__(self, timeout_seconds: float = 30.0):
        # Avoid the module-level SIMBAD client in concurrent workers.
        self.client = configured_simbad_client(Simbad())
        _set_http_timeout(self.client, timeout_seconds)

    def query(self, context: MetadataQueryContext) -> MetadataQueryResult:
        rows = []
        if context.preferred_identifier:
            rows = self._query_core_identifier(context.preferred_identifier)
        if not rows:
            rows = self._query_core_position(context)
        if not rows:
            return MetadataQueryResult("no_match")
        if len(rows) > 1:
            candidates = tuple(self._snapshot(row, enrich=False) for row in rows)
            return MetadataQueryResult("ambiguous", candidates)
        return MetadataQueryResult("match", (self._snapshot(rows[0], enrich=True),))

    def query_many(
        self,
        contexts: tuple[MetadataQueryContext, ...],
    ) -> dict[int, MetadataQueryResult]:
        result = {context.target_id: MetadataQueryResult("no_match") for context in contexts}
        by_identifier = {
            context.preferred_identifier: context
            for context in contexts
            if context.preferred_identifier
        }
        if by_identifier:
            rows_by_identifier = self._query_core_identifiers_many(tuple(by_identifier))
            for identifier, context in by_identifier.items():
                rows = rows_by_identifier.get(identifier, [])
                if not rows:
                    continue
                if len(rows) > 1:
                    result[context.target_id] = MetadataQueryResult(
                        "ambiguous",
                        tuple(self._snapshot(row, enrich=False) for row in rows),
                    )
                else:
                    result[context.target_id] = MetadataQueryResult(
                        "match",
                        (self._snapshot(rows[0], enrich=True),),
                    )
        for context in contexts:
            if result[context.target_id].status != "no_match":
                continue
            # Coordinate-only targets and identifiers absent from SIMBAD still
            # need the established positional fallback.
            if context.preferred_identifier is None:
                result[context.target_id] = self.query(context)
        return result

    def _query_core_identifier(self, identifier: str):
        query = f"""
            SELECT TOP 2 {self.CORE_COLUMNS}
            FROM basic AS b
            JOIN ident AS i ON i.oidref=b.oid
            LEFT JOIN alltypes AS a ON a.oidref=b.oid
            WHERE i.id={_literal(identifier)}
        """
        return self._query_rows(query)

    def _query_core_identifiers_many(self, identifiers: tuple[str, ...]) -> dict[str, list[Any]]:
        if not identifiers:
            return {}
        literals = ", ".join(_literal(identifier) for identifier in identifiers)
        query = f"""
            SELECT i.id AS input_id, {self.CORE_COLUMNS}
            FROM ident AS i
            JOIN basic AS b ON i.oidref=b.oid
            LEFT JOIN alltypes AS a ON a.oidref=b.oid
            WHERE i.id IN ({literals})
        """
        rows = self._query_rows(query)
        normalized = {_identifier_key(identifier): identifier for identifier in identifiers}
        result = {identifier: [] for identifier in identifiers}
        for row in rows:
            input_id = _text(_value(row, "input_id"))
            identifier = normalized.get(_identifier_key(input_id or ""))
            if identifier is not None:
                result[identifier].append(row)
        return result

    def _query_core_position(self, context: MetadataQueryContext):
        radius_deg = self.radius_arcsec / 3600.0
        query = f"""
            SELECT TOP 2 {self.CORE_COLUMNS}
            FROM basic AS b
            LEFT JOIN alltypes AS a ON a.oidref=b.oid
            WHERE 1=CONTAINS(
                POINT('ICRS', b.ra, b.dec),
                CIRCLE('ICRS', {context.astrometry.ra_deg}, {context.astrometry.dec_deg}, {radius_deg})
            )
        """
        return self._query_rows(query)

    @staticmethod
    def parse_core_row(row) -> dict[str, object]:
        raw_types = _text(_value(row, "otypes")) or ""
        return {
            "oid": _int(_value(row, "oid")),
            "main_id": _text(_value(row, "main_id")),
            "ra_deg": _float(_value(row, "ra")),
            "dec_deg": _float(_value(row, "dec")),
            "spectral_type": _text(_value(row, "sp_type")),
            "spectral_type_bibcode": _text(_value(row, "sp_bibcode")),
            "parallax_mas": _float(_value(row, "plx_value")),
            "parallax_error_mas": _float(_value(row, "plx_err")),
            "parallax_bibcode": _text(_value(row, "plx_bibcode")),
            "pm_ra_cosdec_masyr": _float(_value(row, "pmra")),
            "pm_dec_masyr": _float(_value(row, "pmdec")),
            "proper_motion_bibcode": _text(_value(row, "pm_bibcode")),
            "radial_velocity_kms": _float(_value(row, "rvz_radvel")),
            "radial_velocity_error_kms": _float(_value(row, "rvz_err")),
            "radial_velocity_bibcode": _text(_value(row, "rvz_bibcode")),
            "primary_object_type": _text(_value(row, "otype")),
            "object_type_codes": tuple(code for code in raw_types.split("|") if code),
        }

    def _snapshot(self, row, *, enrich: bool) -> SimbadSnapshot:
        core = self.parse_core_row(row)
        if not core["oid"] or not core["main_id"] or core["ra_deg"] is None or core["dec_deg"] is None:
            raise ProviderError("SIMBAD metadata response omitted oid, main_id, or coordinates")
        object_types = ()
        relationships = ()
        identifiers = ()
        raw = {
            "basic": _json_row(row),
            "identifiers": [],
            "object_types": [],
            "relationships": [],
        }
        if enrich:
            identifier_rows = self._identifier_rows(core["oid"])
            identifiers = tuple(
                value
                for value in (_text(_value(row, "id")) for row in identifier_rows)
                if value
            )
            object_types, type_rows = self._object_types(
                core["object_type_codes"], core["primary_object_type"]
            )
            relationship_rows = self._relationship_rows(core["oid"])
            relationships = self.parse_relationship_rows(
                core["ra_deg"], core["dec_deg"], relationship_rows
            )
            raw["identifiers"] = [_json_row(value) for value in identifier_rows]
            raw["object_types"] = [_json_row(value) for value in type_rows]
            raw["relationships"] = [_json_row(value) for value in relationship_rows]
        return SimbadSnapshot(
            oid=core["oid"],
            main_id=core["main_id"],
            ra_deg=core["ra_deg"],
            dec_deg=core["dec_deg"],
            identifiers=identifiers,
            spectral_type=core["spectral_type"],
            spectral_type_bibcode=core["spectral_type_bibcode"],
            parallax_mas=core["parallax_mas"],
            parallax_error_mas=core["parallax_error_mas"],
            parallax_bibcode=core["parallax_bibcode"],
            pm_ra_cosdec_masyr=core["pm_ra_cosdec_masyr"],
            pm_dec_masyr=core["pm_dec_masyr"],
            proper_motion_bibcode=core["proper_motion_bibcode"],
            radial_velocity_kms=core["radial_velocity_kms"],
            radial_velocity_error_kms=core["radial_velocity_error_kms"],
            radial_velocity_bibcode=core["radial_velocity_bibcode"],
            primary_object_type=core["primary_object_type"],
            object_types=object_types,
            relationships=relationships,
            raw=raw,
        )

    def _identifier_rows(self, oid: int):
        return self._query_rows(f"SELECT id FROM ident WHERE oidref={oid}")

    def _object_types(self, codes: tuple[str, ...], primary: str | None):
        if not codes:
            return (), []
        literals = ",".join(_literal(code) for code in codes)
        rows = self._query_rows(
            "SELECT otype, label, description FROM otypedef "
            f"WHERE otype IN ({literals})"
        )
        by_code = {_text(_value(row, "otype")): row for row in rows}
        values = tuple(
            ObjectTypeValue(
                code,
                _text(_value(by_code.get(code, {}), "label")),
                _text(_value(by_code.get(code, {}), "description")),
                code == primary,
            )
            for code in codes
        )
        return values, rows

    def _relationship_rows(self, oid: int):
        parents = self._query_rows(f"""
            SELECT 'parent' AS direction, p.oid AS related_oid,
                   p.main_id AS related_main_id, p.ra AS related_ra,
                   p.dec AS related_dec, p.otype AS related_otype,
                   pa.otypes AS related_otypes, p.sp_type AS related_sp_type,
                   p.sp_bibcode AS related_sp_bibcode,
                   h.membership, h.link_bibcode
            FROM h_link AS h JOIN basic AS p ON p.oid=h.parent
            LEFT JOIN alltypes AS pa ON pa.oidref=p.oid
            WHERE h.child={oid}
        """)
        children = self._query_rows(f"""
            SELECT 'child' AS direction, c.oid AS related_oid,
                   c.main_id AS related_main_id, c.ra AS related_ra,
                   c.dec AS related_dec, c.otype AS related_otype,
                   ca.otypes AS related_otypes, c.sp_type AS related_sp_type,
                   c.sp_bibcode AS related_sp_bibcode,
                   h.membership, h.link_bibcode
            FROM h_link AS h JOIN basic AS c ON c.oid=h.child
            LEFT JOIN alltypes AS ca ON ca.oidref=c.oid
            WHERE h.parent={oid}
        """)
        return [*parents, *children]

    @staticmethod
    def parse_relationship_rows(ra_deg: float, dec_deg: float, rows) -> tuple[RelationshipValue, ...]:
        origin = SkyCoord(ra_deg * u.deg, dec_deg * u.deg, frame="icrs")
        values = []
        for row in rows:
            related_ra = _float(_value(row, "related_ra"))
            related_dec = _float(_value(row, "related_dec"))
            related_types = tuple(
                code for code in ((_text(_value(row, "related_otypes")) or "").split("|"))
                if code
            )
            separation = None
            if related_ra is not None and related_dec is not None:
                related = SkyCoord(related_ra * u.deg, related_dec * u.deg, frame="icrs")
                separation = float(origin.separation(related).arcsec)
            values.append(
                RelationshipValue(
                    direction=_text(_value(row, "direction")),
                    related_oid=_int(_value(row, "related_oid")),
                    related_main_id=_text(_value(row, "related_main_id")),
                    related_ra_deg=related_ra,
                    related_dec_deg=related_dec,
                    membership_percent=_int(_value(row, "membership")),
                    link_bibcode=_text(_value(row, "link_bibcode")),
                    separation_arcsec=separation,
                    related_object_type=_text(_value(row, "related_otype")),
                    related_object_types=related_types,
                    related_spectral_type=_text(_value(row, "related_sp_type")),
                    related_spectral_type_bibcode=_text(_value(row, "related_sp_bibcode")),
                )
            )
        return tuple(values)

    def _query_rows(self, query: str):
        try:
            table = self.client.query_tap(query)
        except Exception as error:
            raise ProviderError(f"SIMBAD metadata query failed: {error}", transient=True) from error
        return [] if table is None else list(table)
