from __future__ import annotations

import json
import math
import re

import astropy.units as u
from astropy.coordinates import SkyCoord
from sqlalchemy import or_, select

from ...astrometry import angular_separation_arcsec
from ..types import (
    CatalogAttributeValue,
    CatalogCandidate,
    CatalogQueryContext,
    MeasurementValue,
)
from ..provenance import (
    CatalogProvenance,
    provenance_from_payload,
    vizier_access_url,
    vizier_entry_url,
    vizier_readme_url,
    with_payload_provenance,
)
from ...providers import Astrometry, ProviderError
from ..reference_definitions import (
    GASPAR_BIBCODE, GASPAR_DEFINITION, IRAS_FSC_BIBCODE,
    IRAS_FSC_DEFINITION, IRAS_PSC_BIBCODE, IRAS_PSC_DEFINITION,
    HIP2_BIBCODE, HIP2_DEFINITION, SNAPSHOT_CATALOGS,
    KOEN10_BIBCODE, KOEN10_DEFINITION,
    PAUNZEN15_BIBCODE, PAUNZEN15_DEFINITION,
    TDSC_BIBCODE, TDSC_DEFINITION, V70A_DEFINITION,
    UBVMEANS_BIBCODE, UBVMEANS_DEFINITION,
    SnapshotCatalogDefinition,
)
from ..ubv_components import decode_ubv_component, ubv_photometry_scope
from ..tdsc_components import decode_tdsc_component
from ..v70a_components import decode_v70a_component
from ...reference.store import (
    ReferenceAlias,
    ReferenceCrossIdentifier,
    ReferenceRow,
    ReferenceStore,
    ReferenceTable,
    _star_identifier,
)
from .vizier import row_float, row_text
from .review_metadata import add_review_metadata, PositionUncertainty

class SnapshotCatalogAdapter:
    """Shared local-snapshot matching; subclasses declare extracted science."""

    def __init__(self, store: ReferenceStore, definition: SnapshotCatalogDefinition):
        self.store = store
        self.definition = definition
        self.name = definition.adapter
        self.query_epoch = definition.query_epoch
        self.radius_arcsec = definition.radius_arcsec
        snapshot = store.current_snapshot(self.name)
        if snapshot is None:
            raise RuntimeError(
                f"{self.name} reference snapshot is missing; "
                f"run 'sdb reference fetch {self.name}'"
            )
        self.snapshot_id = snapshot.id
        self.snapshot_catalog = snapshot.catalog
        self.snapshot_source_url = snapshot.source_url
        self.snapshot_digest = snapshot.content_sha256
        revision = (
            ""
            if definition.application_revision is None
            else f"+{definition.application_revision}"
        )
        self.release = (
            f"{definition.catalog}@{snapshot.content_sha256[:16]}{revision}"
        )
        self.application_revision = definition.application_revision
        self.store.materialize_cross_identifiers(self.name)

    def _snapshot_context(self):
        # Select only explicitly declared science/matching tables. Related
        # reference tables are loaded solely through declared relationships.
        with self.store.sessions() as session:
            tables = list(session.scalars(select(ReferenceTable).where(
                ReferenceTable.snapshot_id == self.snapshot_id,
                ReferenceTable.name.in_(self.definition.tables_for_matching),
            )))
            if len(tables) != len(self.definition.tables_for_matching):
                raise ProviderError(
                    f"{self.name} snapshot is missing one or more match tables"
                )
            refs = {}
            for relationship in self.definition.relationships:
                refs_table = session.scalar(select(ReferenceTable).where(
                    ReferenceTable.snapshot_id == self.snapshot_id,
                    ReferenceTable.name == relationship.to_table,
                ))
                if refs_table is not None:
                    for ref_row in session.scalars(select(ReferenceRow).where(
                        ReferenceRow.table_id == refs_table.id
                    )):
                        payload = json.loads(ref_row.payload_json)
                        refs[int(payload[relationship.to_column])] = payload
        return (
            tuple(table.id for table in tables),
            refs,
            {table.id: table for table in tables},
        )

    def _nearby_rows(self, table_id: int, context: CatalogQueryContext):
        radius_deg = self.radius_arcsec / 3600.0
        dec = context.astrometry.dec_deg
        dec_min = max(-90.0, dec - radius_deg)
        dec_max = min(90.0, dec + radius_deg)
        cos_dec = abs(math.cos(math.radians(dec)))
        ra_radius = 180.0 if cos_dec < 1e-6 else min(180.0, radius_deg / cos_dec)
        ra = context.astrometry.ra_deg % 360.0
        if ra_radius >= 180.0:
            ra_clause = ReferenceRow.ra_deg.is_not(None)
        else:
            ra_min = (ra - ra_radius) % 360.0
            ra_max = (ra + ra_radius) % 360.0
            if ra_min <= ra_max:
                ra_clause = ReferenceRow.ra_deg.between(ra_min, ra_max)
            else:
                ra_clause = or_(
                    ReferenceRow.ra_deg >= ra_min,
                    ReferenceRow.ra_deg <= ra_max,
                )
        with self.store.sessions() as session:
            return list(session.scalars(select(ReferenceRow).where(
                ReferenceRow.table_id == table_id,
                ReferenceRow.dec_deg.between(dec_min, dec_max),
                ra_clause,
            )))

    def _alias_rows(self, table_id: int, context: CatalogQueryContext):
        aliases = {
            _star_identifier(value) for value in context.identifiers
            if _star_identifier(value)
        }
        if not aliases:
            return []
        with self.store.sessions() as session:
            row_ids = list(session.scalars(
                select(ReferenceAlias.row_id).where(
                    ReferenceAlias.normalized_identifier.in_(aliases)
                )
            ))
            if not row_ids:
                return []
            return list(session.scalars(select(ReferenceRow).where(
                ReferenceRow.id.in_(row_ids),
                ReferenceRow.table_id == table_id,
            )))

    def _alias_rows_many(
        self,
        table_ids: tuple[int, ...],
        contexts: list[CatalogQueryContext],
    ) -> dict[int, list[ReferenceRow]]:
        """Resolve all target aliases once instead of scanning a table per target."""
        targets_by_alias: dict[str, set[int]] = {}
        for context in contexts:
            for value in context.identifiers:
                alias = _star_identifier(value)
                if alias:
                    targets_by_alias.setdefault(alias, set()).add(context.target_id)
        result: dict[int, dict[int, ReferenceRow]] = {
            context.target_id: {} for context in contexts
        }
        if not targets_by_alias:
            return {target_id: [] for target_id in result}

        aliases_by_row: dict[int, set[str]] = {}
        alias_values = sorted(targets_by_alias)
        with self.store.sessions() as session:
            for offset in range(0, len(alias_values), 500):
                chunk = alias_values[offset:offset + 500]
                for alias, row_id in session.execute(select(
                    ReferenceAlias.normalized_identifier,
                    ReferenceAlias.row_id,
                ).where(ReferenceAlias.normalized_identifier.in_(chunk))):
                    aliases_by_row.setdefault(row_id, set()).add(alias)
            row_ids = sorted(aliases_by_row)
            rows_by_id: dict[int, ReferenceRow] = {}
            for offset in range(0, len(row_ids), 500):
                chunk = row_ids[offset:offset + 500]
                for row in session.scalars(select(ReferenceRow).where(
                    ReferenceRow.id.in_(chunk),
                    ReferenceRow.table_id.in_(table_ids),
                )):
                    rows_by_id[row.id] = row

        for row_id, aliases in aliases_by_row.items():
            row = rows_by_id.get(row_id)
            if row is None:
                continue
            for alias in aliases:
                for target_id in targets_by_alias[alias]:
                    result[target_id][row.id] = row
        return {
            target_id: list(rows.values()) for target_id, rows in result.items()
        }

    def _cross_identifier_rows_many(
        self,
        table_ids: tuple[int, ...],
        contexts: list[CatalogQueryContext],
    ) -> dict[int, list[ReferenceRow]]:
        targets_by_alias: dict[str, set[int]] = {}
        for context in contexts:
            for value in context.identifiers:
                alias = _star_identifier(value)
                if alias:
                    targets_by_alias.setdefault(alias, set()).add(
                        context.target_id
                    )
        result: dict[int, dict[int, ReferenceRow]] = {
            context.target_id: {} for context in contexts
        }
        if not targets_by_alias:
            return {target_id: [] for target_id in result}
        with self.store.sessions() as session:
            links = list(session.execute(select(
                ReferenceCrossIdentifier.normalized_identifier,
                ReferenceRow,
            ).join(
                ReferenceRow,
                ReferenceRow.id == ReferenceCrossIdentifier.row_id,
            ).where(
                ReferenceCrossIdentifier.normalized_identifier.in_(
                    tuple(targets_by_alias)
                ),
                ReferenceRow.table_id.in_(table_ids),
            )))
        for alias, row in links:
            for target_id in targets_by_alias[alias]:
                result[target_id][row.id] = row
        return {
            target_id: list(rows.values()) for target_id, rows in result.items()
        }

    def _candidate(self, row, refs, table: ReferenceTable) -> CatalogCandidate:
        # Common candidate envelope. The concrete adapter methods below own
        # payload enrichment, epoch, photometry, and auxiliary attributes.
        payload = json.loads(row.payload_json)
        payload = self.enrich_payload(payload, refs)
        native_identifiers = self.store.cross_identifiers(row.id)
        if native_identifiers:
            payload["_sdb_native_identifiers"] = native_identifiers
        identifier_column, identifier_value = self._provenance_identifier(
            payload
        )
        provenance = (CatalogProvenance(
            service="local reference snapshot",
            catalog_id=self.snapshot_catalog,
            table_id=table.name,
            row_key=row.stable_key or f"row:{row.row_number}",
            identifier_column=identifier_column,
            identifier_value=identifier_value,
            source_url=self.snapshot_source_url,
            access_url=(
                vizier_entry_url(
                    table.name, identifier_column, identifier_value
                )
                if identifier_column and identifier_value
                else vizier_access_url(table.name)
            ),
            readme_url=vizier_readme_url(self.snapshot_catalog),
        ),)
        payload = with_payload_provenance(payload, provenance)
        return CatalogCandidate(
            source_id=self.definition.row_key(
                payload, f"{self.name}:{row.row_number}"
            ),
            ra_deg=row.ra_deg,
            dec_deg=row.dec_deg,
            epoch=self.candidate_epoch(payload),
            payload=payload,
            measurements=self.measurements(payload),
            attributes=self.attributes(payload),
            provenance=provenance,
        )

    def _provenance_identifier(
        self,
        payload: dict[str, object],
    ) -> tuple[str | None, str | None]:
        column = self.definition.primary_identifier
        value = row_text(payload, column)
        composite_labels = {
            label
            for label, _columns, _separator
            in self.definition.composite_identifier_columns
        }
        # Some VizieR tables expose a literal label (for example ``TYC``)
        # beside the actual multi-column identifier. It is display metadata,
        # not a row locator.
        if (
            column in composite_labels
            and value
            and value.casefold() == column.casefold()
        ):
            return None, None
        return (column, value) if value else (None, None)

    def enrich_payload(self, payload, refs):
        return payload

    def candidate_epoch(self, payload) -> float:
        return self.query_epoch

    def measurements(self, payload) -> tuple[MeasurementValue, ...]:
        # Subclasses explicitly opt in each provider-native band or color.
        return ()

    def attributes(self, payload) -> tuple[CatalogAttributeValue, ...]:
        # Non-photometric provider values are likewise opt-in.
        return ()

    def query_many(
        self, contexts: list[CatalogQueryContext]
    ) -> dict[int, list[CatalogCandidate]]:
        table_ids, refs, tables_by_id = self._snapshot_context()
        result = {context.target_id: [] for context in contexts}
        alias_rows_by_target = self._alias_rows_many(table_ids, contexts)
        cross_rows_by_target = self._cross_identifier_rows_many(
            table_ids, contexts,
        )
        for context in contexts:
            rows = {
                row.id: row for row in alias_rows_by_target[context.target_id]
            }
            rows.update({
                row.id: row
                for row in cross_rows_by_target[context.target_id]
            })
            for table_id in table_ids:
                rows.update({
                    row.id: row
                    for row in self._nearby_rows(table_id, context)
                })
            for row in rows.values():
                if row.ra_deg is None or row.dec_deg is None:
                    continue
                candidate = self._candidate(row, refs, tables_by_id[row.table_id])
                separation = angular_separation_arcsec(
                    context.astrometry,
                    candidate.astrometry,
                    epoch=self.query_epoch,
                )
                payload = candidate.payload
                row_aliases = {
                    _star_identifier(value)
                    for value in self.definition.identifiers(payload)
                }
                row_aliases.discard(None)
                target_aliases = {
                    _star_identifier(value) for value in context.identifiers
                }
                target_aliases.discard(None)
                matched_aliases = sorted(row_aliases & target_aliases)
                name_match = bool(matched_aliases)
                if separation <= self.radius_arcsec or name_match:
                    associated_payload = dict(candidate.payload)
                    associated_payload["_sdb_association"] = {
                        "method": "position+identifier" if name_match else "position",
                        "identifier_agreement": name_match,
                        "matched_identifiers": matched_aliases,
                        "catalog_identifiers": sorted(row_aliases),
                    }
                    result[context.target_id].append(CatalogCandidate(
                        source_id=candidate.source_id,
                        ra_deg=candidate.ra_deg,
                        dec_deg=candidate.dec_deg,
                        epoch=candidate.epoch,
                        payload=associated_payload,
                        measurements=candidate.measurements,
                        attributes=candidate.attributes,
                        detection_key=candidate.detection_key,
                        provenance=candidate.provenance,
                    ))
        return result

    def query(self, context: CatalogQueryContext) -> list[CatalogCandidate]:
        return self.query_many([context])[context.target_id]

    def candidate_from_payload(self, payload: dict[str, object]) -> CatalogCandidate:
        ra, dec = self.definition.position(payload)
        if ra is None or dec is None:
            raise ValueError(f"{self.name} payload has no usable coordinates")
        enriched = dict(payload)
        if self.name != "gaspar13" or "_resolved_age_references" not in enriched:
            enriched = self.enrich_payload(enriched, {})
        provenance = provenance_from_payload(enriched)
        if not provenance:
            stable_key = self.definition.row_key(enriched, self.name)
            with self.store.sessions() as session:
                rows = list(session.execute(
                    select(ReferenceRow, ReferenceTable)
                    .join(ReferenceTable, ReferenceTable.id == ReferenceRow.table_id)
                    .where(
                        ReferenceTable.snapshot_id == self.snapshot_id,
                        ReferenceTable.name.in_(
                            self.definition.tables_for_matching
                        ),
                        ReferenceRow.stable_key == stable_key,
                    )
                ))
            identifier_column, identifier_value = (
                self._provenance_identifier(enriched)
            )
            provenance = tuple(CatalogProvenance(
                service="local reference snapshot",
                catalog_id=self.snapshot_catalog,
                table_id=table.name,
                row_key=row.stable_key or f"row:{row.row_number}",
                identifier_column=identifier_column,
                identifier_value=identifier_value,
                source_url=self.snapshot_source_url,
                access_url=(
                    vizier_entry_url(
                        table.name,
                        identifier_column,
                        identifier_value,
                    )
                    if identifier_column and identifier_value
                    else vizier_access_url(table.name)
                ),
                readme_url=vizier_readme_url(self.snapshot_catalog),
            ) for row, table in rows)
            enriched = with_payload_provenance(enriched, provenance)
        return CatalogCandidate(
            source_id=self.definition.row_key(enriched, self.name),
            ra_deg=ra,
            dec_deg=dec,
            epoch=self.candidate_epoch(enriched),
            payload=enriched,
            measurements=self.measurements(enriched),
            attributes=self.attributes(enriched),
            provenance=provenance,
        )

    @staticmethod
    def normalize(candidate: CatalogCandidate) -> tuple[MeasurementValue, ...]:
        return candidate.measurements


class GasparSnapshotAdapter(SnapshotCatalogAdapter):
    def __init__(self, store: ReferenceStore):
        super().__init__(store, GASPAR_DEFINITION)

    def enrich_payload(self, payload, refs):
        codes = [
            int(value)
            for value in re.findall(r"\d+", str(payload.get("r_Age") or ""))
        ]
        payload["_resolved_age_references"] = [
            refs[code] for code in codes if code in refs
        ]
        return payload

    def measurements(self, payload):
        flux = row_float(payload, "F70")
        if flux is None:
            return ()
        return (MeasurementValue(
            band="MIPS70",
            value=flux,
            error=row_float(payload, "e_F70") or 0.0,
            systematic_error=flux * 0.05,
            unit="mJy",
            bibcode=GASPAR_BIBCODE,
            note1=f"Xs:{row_text(payload, 'chi70') or ''}",
            note2=f"AgeFlg:{row_text(payload, 'q_Age') or ''}",
            resolution_major_arcsec=18.0,
            resolution_minor_arcsec=18.0,
            resolution_kind="psf_fwhm",
            resolution_reference="Spitzer MIPS Instrument Handbook",
        ),)


class V70ASnapshotAdapter(SnapshotCatalogAdapter):
    """Gliese nearby-star snapshot; legacy photometric mappings remain disabled."""

    def __init__(self, store: ReferenceStore):
        super().__init__(store, V70A_DEFINITION)

    def enrich_payload(self, payload, refs):
        payload["_legacy_disabled_bands"] = {
            "VJ": "Vmag",
            "BJ_VJ": "B-V",
            "UJ_BJ": "U-B",
            "RC_IC": "R-I",
        }
        return payload

    @staticmethod
    def score_candidate(context, candidate, separation_arcsec):
        association = candidate.payload.get("_sdb_association", {})
        if association.get("identifier_agreement") and separation_arcsec <= 120.0:
            return 1.0
        positional = math.exp(-0.5 * (separation_arcsec / 2.0) ** 2)
        # A component row must not become an automatic match for a nearby
        # composite/system target. Position remains useful review evidence,
        # but the component-qualified Gliese name is the secure identity.
        if decode_v70a_component(candidate.payload, candidate.source_id).native_code:
            return min(positional, 0.45)
        return positional

    def attributes(self, payload):
        values = []

        def text(key, column, *, quality=None, reference=None, note=None):
            value = row_text(payload, column)
            if value:
                values.append(CatalogAttributeValue(
                    key=key,
                    value_text=value,
                    quality=row_text(payload, quality) if quality else None,
                    reference=row_text(payload, reference) if reference else None,
                    note=row_text(payload, note) if note else None,
                ))

        def number(
            key, column, unit, *, error=None, quality=None, reference=None, note=None
        ):
            value = row_float(payload, column)
            if value is not None:
                values.append(CatalogAttributeValue(
                    key=key,
                    value_float=value,
                    uncertainty=row_float(payload, error) if error else None,
                    unit=unit,
                    quality=row_text(payload, quality) if quality else None,
                    reference=row_text(payload, reference) if reference else None,
                    note=row_text(payload, note) if note else None,
                ))

        text("spectral_type", "Sp", reference="r_Sp")
        number("parallax", "plx", "mas", error="e_plx", quality="n_plx")
        number(
            "trigonometric_parallax", "trplx", "mas",
            error="e_trplx", quality="n_plx",
        )
        number("radial_velocity", "RV", "km/s", note="n_RV")
        number("proper_motion_total", "pm", "arcsec/yr", quality="u_pm")
        number("proper_motion_position_angle", "pmPA", "deg")
        number("v_magnitude", "Vmag", "mag", reference="r_Vmag", note="n_Vmag")
        number("b_minus_v", "B-V", "mag", reference="r_B-V", note="n_B-V")
        number("u_minus_b", "U-B", "mag", reference="r_U-B", note="n_U-B")
        number("r_minus_i", "R-I", "mag", reference="r_R-I", note="n_R-I")
        number("absolute_v_magnitude", "Mv", "mag", quality="q_Mv", note="n_Mv")
        number("galactic_velocity_u", "U", "km/s")
        number("galactic_velocity_v", "V", "km/s")
        number("galactic_velocity_w", "W", "km/s")
        return tuple(values)


def _first_float(payload, *columns):
    for column in columns:
        value = row_float(payload, column)
        if value is not None:
            return value
    return None


def _first_text(payload, *columns):
    for column in columns:
        value = row_text(payload, column)
        if value:
            return value
    return None


class IrasSnapshotAdapter(SnapshotCatalogAdapter):
    """Shared normalization for the complete IRAS PSC and FSC snapshots."""

    resolutions = {12: 30.0, 25: 30.0, 60: 60.0, 100: 120.0}
    # Major and Minor are 1-sigma semiaxes. A three-sigma ellipse contains
    # about 98.9% of a two-dimensional Gaussian positional distribution.
    acceptance_score = math.exp(-0.5 * 3.0**2)
    review_fields = ()
    position_uncertainty = PositionUncertainty(
        major_columns=("Major",),
        minor_columns=("Minor",),
        position_angle_columns=("PosAng",),
        scale_to_arcsec=1.0,
    )

    def enrich_payload(self, payload, refs):
        return add_review_metadata(
            payload,
            position_uncertainty=self.position_uncertainty,
        )

    @staticmethod
    def score_candidate(context, candidate, separation_arcsec):
        association = candidate.payload.get("_sdb_association", {})
        if association.get("identifier_agreement"):
            return 1.0
        major = row_float(candidate.payload, "Major")
        minor = row_float(candidate.payload, "Minor")
        position_angle = row_float(candidate.payload, "PosAng")
        if not major or not minor or position_angle is None:
            return math.exp(-0.5 * (separation_arcsec / 2.0) ** 2)
        source = SkyCoord(candidate.ra_deg * u.deg, candidate.dec_deg * u.deg)
        target = SkyCoord(
            context.astrometry.ra_deg * u.deg,
            context.astrometry.dec_deg * u.deg,
        )
        east, north = source.spherical_offsets_to(target)
        east_arcsec = east.to_value(u.arcsec)
        north_arcsec = north.to_value(u.arcsec)
        angle = math.radians(position_angle)
        along_major = east_arcsec * math.sin(angle) + north_arcsec * math.cos(angle)
        along_minor = east_arcsec * math.cos(angle) - north_arcsec * math.sin(angle)
        normalized_squared = (along_major / major) ** 2 + (along_minor / minor) ** 2
        return math.exp(-0.5 * normalized_squared)

    def measurements(self, payload):
        values = []
        for wavelength, band in ((12, "IRAS12"), (25, "IRAS25"), (60, "IRAS60"), (100, "IRAS100")):
            flux = _first_float(payload, f"Fnu_{wavelength}", f"Fnu{wavelength}")
            if flux is None:
                continue
            quality = _first_text(
                payload, f"q_Fnu_{wavelength}", f"q_Fnu{wavelength}",
                f"q_{{Fnu}}{wavelength}",
            ) or ""
            relative_error = _first_float(
                payload, f"e_Fnu_{wavelength}", f"e_Fnu{wavelength}",
                f"e_{{Fnu}}{wavelength}",
            )
            upper_limit = quality == "1"
            confusion = _first_text(payload, "Confuse", "Conf") or ""
            provider_flagged = confusion not in {"", "0", "0000"}
            values.append(MeasurementValue(
                band=band,
                value=flux,
                error=0.0 if relative_error is None else flux * relative_error / 100.0,
                unit="Jy",
                bibcode=self.definition.bibliography,
                quality=quality or None,
                upper_limit=upper_limit,
                note1=f"FQual:{quality}",
                note2=f"Confuse:{confusion}",
                resolution_major_arcsec=self.resolutions[wavelength],
                resolution_minor_arcsec=self.resolutions[wavelength],
                resolution_kind="catalog_extent_limit_in_scan",
                resolution_reference="IRAS Catalogs and Atlases Explanatory Supplement",
                blend_state="blended" if provider_flagged else "clear",
                blend_reason="provider_flagged" if provider_flagged else None,
            ))
        return tuple(values)


class IrasPscSnapshotAdapter(IrasSnapshotAdapter):
    def __init__(self, store: ReferenceStore):
        super().__init__(store, IRAS_PSC_DEFINITION)


class IrasFscSnapshotAdapter(IrasSnapshotAdapter):
    def __init__(self, store: ReferenceStore):
        super().__init__(store, IRAS_FSC_DEFINITION)


class Hip2SnapshotAdapter(SnapshotCatalogAdapter):
    def __init__(self, store: ReferenceStore):
        super().__init__(store, HIP2_DEFINITION)

    def measurements(self, payload):
        magnitude = row_float(payload, "Hpmag")
        if magnitude is None:
            return ()
        return (MeasurementValue(
            band="HP",
            value=magnitude,
            error=row_float(payload, "e_Hpmag") or 0.0,
            unit="mag",
            bibcode=HIP2_BIBCODE,
            quality=row_text(payload, "Sn"),
            note1=f"sHp:{row_text(payload, 'sHp') or ''}",
            note2=f"VA:{row_text(payload, 'VA') or ''}",
        ),)


class TdscSnapshotAdapter(SnapshotCatalogAdapter):
    def __init__(self, store: ReferenceStore):
        super().__init__(store, TDSC_DEFINITION)

    def enrich_payload(self, payload, refs):
        payload["_sdb_photometry_scope"] = decode_tdsc_component(payload).as_dict()
        return payload

    @staticmethod
    def score_candidate(context, candidate, separation_arcsec):
        association = candidate.payload.get("_sdb_association", {})
        matched = association.get("matched_identifiers", ())
        if (
            separation_arcsec <= 120.0
            and any(value.startswith(("HD ", "TYC ")) for value in matched)
        ):
            return 1.0
        positional = math.exp(-0.5 * (separation_arcsec / 2.0) ** 2)
        # HIP can identify a system rather than an individual TDSC component.
        # It corroborates a close positional match but cannot select a distant
        # component by itself.
        hip_evidence = 0.25 if any(value.startswith("HIP ") for value in matched) else 0.0
        return min(positional + hip_evidence, 1.0)

    def measurements(self, payload):
        flag = row_text(payload, "magflg") or ""
        values = []
        bt = row_float(payload, "BTmag")
        if bt is not None:
            values.append(MeasurementValue(
                band="BT",
                value=bt,
                error=row_float(payload, "e_BTmag") or 0.0,
                unit="mag",
                bibcode=TDSC_BIBCODE,
                quality=flag or None,
                note1=f"magflg:{flag}",
                note2=f"component:{row_text(payload, 'm_TDSC') or ''}",
            ))
        vt = row_float(payload, "VTmag")
        if vt is not None and flag not in {"B", "H"}:
            values.append(MeasurementValue(
                band="VT",
                value=vt,
                error=row_float(payload, "e_VTmag") or 0.0,
                unit="mag",
                bibcode=TDSC_BIBCODE,
                quality=flag or None,
                note1=f"magflg:{flag}",
                note2=f"component:{row_text(payload, 'm_TDSC') or ''}",
            ))
        return tuple(values)


def _valid_catalog_float(payload, column):
    """Return a finite catalogue value, excluding common numeric null sentinels."""
    value = row_float(payload, column)
    if value is None or not math.isfinite(value) or value <= -9.0:
        return None
    return value


class UbvMeansSnapshotAdapter(SnapshotCatalogAdapter):
    """Mermilliod homogeneous photoelectric UBV means (VizieR II/168)."""

    def __init__(self, store: ReferenceStore):
        super().__init__(store, UBVMEANS_DEFINITION)

    def enrich_payload(self, payload, refs):
        payload["_sdb_photometry_scope"] = decode_ubv_component(payload).as_dict()
        return payload

    def measurements(self, payload):
        component = row_text(payload, "m_LID") or ""
        ownership_scope, blend_state, blend_reason = ubv_photometry_scope(payload)
        values = []
        for band, column, error, flag, observations, systematic in (
            ("VJ", "Vmag", "e_Vmag", "n_Vmag", "o_Vmag", 0.02),
            ("BJ_VJ", "B-V", "e_B-V", "n_B-V", "o_B-V", 0.02),
            ("UJ_BJ", "U-B", "e_U-B", "n_U-B", "o_U-B", 0.028),
        ):
            value = _valid_catalog_float(payload, column)
            if value is None:
                continue
            values.append(MeasurementValue(
                band=band,
                value=value,
                error=_valid_catalog_float(payload, error) or 0.0,
                systematic_error=systematic,
                unit="mag",
                bibcode=UBVMEANS_BIBCODE,
                quality=row_text(payload, flag),
                note1=f"component:{component}",
                note2=f"observations:{row_text(payload, observations) or ''}",
                ownership_scope=ownership_scope,
                blend_state=blend_state,
                blend_reason=blend_reason,
            ))
        return tuple(values)


class Paunzen15SnapshotAdapter(SnapshotCatalogAdapter):
    """Paunzen compiled Stroemgren-Crawford catalogue."""

    def __init__(self, store: ReferenceStore):
        super().__init__(store, PAUNZEN15_DEFINITION)

    def measurements(self, payload):
        beta = _valid_catalog_float(payload, "beta")
        beta_note = "" if beta is None else f"beta:{beta:g}"
        values = []
        for band, column, error, observations in (
            ("BS_YS", "b-y", "e_b-y", "o_b-y"),
            ("STROMM1", "m1", "e_m1", "o_m1"),
            ("STROMC1", "c1", "e_c1", "o_c1"),
        ):
            value = _valid_catalog_float(payload, column)
            if value is None:
                continue
            values.append(MeasurementValue(
                band=band,
                value=value,
                error=_valid_catalog_float(payload, error) or 0.0,
                systematic_error=0.01,
                unit="mag",
                bibcode=PAUNZEN15_BIBCODE,
                note1=f"observations:{row_text(payload, observations) or ''}",
                note2=beta_note,
                resolution_major_arcsec=0.8,
                resolution_minor_arcsec=0.8,
                resolution_kind="catalog_spatial_resolution_limit",
                resolution_reference=PAUNZEN15_BIBCODE,
            ))
        return tuple(values)


class Koen10SnapshotAdapter(SnapshotCatalogAdapter):
    """Koen et al. homogeneous SAAO UBV(RI)c photometry."""

    def __init__(self, store: ReferenceStore):
        super().__init__(store, KOEN10_DEFINITION)

    def measurements(self, payload):
        observations = row_text(payload, "n") or ""
        variable = row_text(payload, "Var") or ""
        multiplicity = row_text(payload, "Mlt") or ""
        values = []
        for band, column in (
            ("VJ", "Vmag"),
            ("BJ_VJ", "B-V"),
            ("UJ_BJ", "U-B"),
            ("VJ_RC", "V-Rc"),
            ("VJ_IC", "V-Ic"),
        ):
            value = _valid_catalog_float(payload, column)
            if value is None:
                continue
            values.append(MeasurementValue(
                band=band,
                value=value,
                systematic_error=0.02,
                unit="mag",
                bibcode=KOEN10_BIBCODE,
                quality=variable or None,
                note1=f"observations:{observations}",
                note2=f"multiplicity:{multiplicity}",
                resolution_major_arcsec=30.0,
                resolution_minor_arcsec=30.0,
                resolution_kind="photometric_aperture_diameter",
                resolution_reference=KOEN10_BIBCODE,
            ))
        return tuple(values)

    def attributes(self, payload):
        values = []
        for key, column in (
            ("variability_flag", "Var"),
            ("multiplicity_flag", "Mlt"),
            ("spectral_type", "SpType"),
        ):
            value = row_text(payload, column)
            if value:
                values.append(CatalogAttributeValue(key=key, value_text=value))
        return tuple(values)


def snapshot_adapter(adapter: str, store: ReferenceStore) -> SnapshotCatalogAdapter:
    adapters = {
        "gaspar13": GasparSnapshotAdapter,
        "v70a": V70ASnapshotAdapter,
        "iras_psc": IrasPscSnapshotAdapter,
        "iras_fsc": IrasFscSnapshotAdapter,
        "hip2": Hip2SnapshotAdapter,
        "tdsc": TdscSnapshotAdapter,
        "ubvmeans": UbvMeansSnapshotAdapter,
        "paunzen15": Paunzen15SnapshotAdapter,
        "koen10": Koen10SnapshotAdapter,
    }
    return adapters[adapter](store)
