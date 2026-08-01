from __future__ import annotations

import hashlib
import gzip
import json
import re
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from astropy.table import Table
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, bindparam, create_engine, or_, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from ..cache_store import CachedSnapshotData, SnapshotCache
from ..catalogs.provenance import materialize_catalog_documentation
from ..providers import ProviderError
from ..catalogs.reference_definitions import SNAPSHOT_CATALOGS
from ..serialization import safe_json as _safe_json
from ..identifiers import normalize_identifier
from ..snapshots import SnapshotClient, VizierSnapshotClient as AstroquerySnapshotClient
from ..serialization import row_float, row_payload, row_text

def utcnow():
    return datetime.now(timezone.utc)


class ReferenceBase(DeclarativeBase):
    pass


class ReferenceSnapshot(ReferenceBase):
    __tablename__ = "reference_snapshots"
    __table_args__ = (UniqueConstraint("catalog", "content_sha256"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    catalog: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    readme: Mapped[str] = mapped_column(Text, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ReferenceTable(ReferenceBase):
    __tablename__ = "reference_tables"
    __table_args__ = (UniqueConstraint("snapshot_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("reference_snapshots.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)


class ReferenceColumn(ReferenceBase):
    __tablename__ = "reference_columns"
    __table_args__ = (UniqueConstraint("table_id", "position"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("reference_tables.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    datatype: Mapped[str] = mapped_column(String(100), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(100))
    ucd: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)


class ReferenceRow(ReferenceBase):
    __tablename__ = "reference_rows"
    __table_args__ = (UniqueConstraint("table_id", "row_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("reference_tables.id"), nullable=False, index=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_identifier: Mapped[str | None] = mapped_column(String(200), index=True)
    normalized_identifier: Mapped[str | None] = mapped_column(String(200), index=True)
    ra_deg: Mapped[float | None] = mapped_column(Float, index=True)
    dec_deg: Mapped[float | None] = mapped_column(Float, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    stable_key: Mapped[str | None] = mapped_column(String(300), index=True)
    row_sha256: Mapped[str | None] = mapped_column(String(64))


class ReferenceAlias(ReferenceBase):
    __tablename__ = "reference_aliases"
    __table_args__ = (UniqueConstraint("row_id", "normalized_identifier"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    row_id: Mapped[int] = mapped_column(ForeignKey("reference_rows.id"), nullable=False, index=True)
    normalized_identifier: Mapped[str] = mapped_column(String(200), nullable=False, index=True)


class ReferenceRelationship(ReferenceBase):
    __tablename__ = "reference_relationships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("reference_snapshots.id"), nullable=False, index=True)
    from_table: Mapped[str] = mapped_column(String(150), nullable=False)
    from_column: Mapped[str] = mapped_column(String(100), nullable=False)
    to_table: Mapped[str] = mapped_column(String(150), nullable=False)
    to_column: Mapped[str] = mapped_column(String(100), nullable=False)
    parser: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class CdsBulkSnapshotClient:
    """Download static CDS catalog files directly, avoiding long VizieR jobs."""

    provider = "cds"

    files = {
        "II/125": (
            ("main.dat.gz", "II/125/main", "IRAS Point Sources"),
            ("assoc.dat.gz", "II/125/assoc", "IRAS associations"),
        ),
        "II/156A": (
            ("main.dat.gz", "II/156A/main", "IRAS Faint Sources"),
            ("assoc.dat.gz", "II/156A/assoc", "IRAS FSC associations"),
        ),
        "I/311": (
            ("hip2.dat.gz", "I/311/hip2", "Hipparcos new reduction"),
            ("hip7p.dat", "I/311/hip7p", "Seven-parameter solutions"),
            ("hip9p.dat", "I/311/hip9p", "Nine-parameter solutions"),
            ("hipvim.dat", "I/311/hipvim", "Variability-induced solutions"),
        ),
        "I/276": (
            ("catalog.dat.gz", "I/276/catalog", "TDSC main catalogue"),
            ("supplem.dat.gz", "I/276/supplem", "TDSC supplement"),
            ("notes.dat.gz", "I/276/notes", "TDSC notes"),
        ),
    }

    def __init__(self):
        self._readmes: dict[str, str] = {}

    @staticmethod
    def source_url(catalog: str) -> str:
        return f"https://cdsarc.cds.unistra.fr/ftp/{catalog}/"

    @staticmethod
    def _url(catalog: str, filename: str) -> str:
        return f"https://cdsarc.cds.unistra.fr/ftp/{catalog}/{filename}"

    def fetch_tables(self, catalog: str):
        if catalog not in self.files:
            raise KeyError(f"no CDS bulk-file definition for {catalog}")
        with tempfile.TemporaryDirectory(prefix="sdb-cds-") as directory:
            root = Path(directory)
            readme_path = root / "ReadMe"
            with urllib.request.urlopen(self._url(catalog, "ReadMe"), timeout=60) as response:
                readme = response.read().decode("utf-8")
            # II/125 contains the historical typo "Byte-per-byte", which the
            # Astropy CDS reader does not recognize. Preserve the original in
            # snapshot metadata but normalize the temporary parser copy.
            parser_readme = readme.replace("Byte-per-byte", "Byte-by-byte")
            if catalog == "I/311":
                parser_readme = re.sub(
                    r"\b(\d+)F(\d+)\.\d+(\s+---\s+UW\b)",
                    lambda match: (
                        f"A{int(match.group(1)) * int(match.group(2))}"
                        f"{match.group(3)}"
                    ),
                    parser_readme,
                )
            readme_path.write_text(parser_readme)
            self._readmes[catalog] = readme
            tables = []
            for filename, table_name, description in self.files[catalog]:
                path = root / filename.removesuffix(".gz")
                download_path = root / filename
                with urllib.request.urlopen(
                    self._url(catalog, filename), timeout=120
                ) as response:
                    with download_path.open("wb") as output:
                        shutil.copyfileobj(response, output)
                if filename.endswith(".gz"):
                    with gzip.open(download_path, "rb") as source, path.open("wb") as output:
                        shutil.copyfileobj(source, output)
                table_readme = readme_path
                if catalog == "I/276" and path.name in {"catalog.dat", "supplem.dat"}:
                    table_readme = root / f"{path.name}.ReadMe"
                    table_readme.write_text(re.sub(
                        r"Byte-by-byte Description of files:\s+catalog\.dat,\s+supplem\.dat",
                        f"Byte-by-byte Description of file: {path.name}",
                        parser_readme,
                    ))
                table = Table.read(path, format="ascii.cds", readme=table_readme)
                table.meta["name"] = table_name
                table.meta["description"] = description
                tables.append(table)
            return tables

    def fetch_readme(self, catalog: str) -> str:
        if catalog in self._readmes:
            return self._readmes[catalog]
        with urllib.request.urlopen(self._url(catalog, "ReadMe"), timeout=60) as response:
            return response.read().decode("utf-8")


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: int
    catalog: str
    content_sha256: str
    table_count: int
    row_count: int
    unchanged: bool


def _cached_tables_as_astropy(snapshot: CachedSnapshotData) -> list[Table]:
    """Rehydrate cached provider rows for the existing reference import path."""

    tables: list[Table] = []
    for cached_table in snapshot.tables:
        column_metadata = list(cached_table.metadata.get("columns", []))
        names = [str(column["name"]) for column in column_metadata if "name" in column]
        if not names and cached_table.rows:
            names = list(cached_table.rows[0])
        rows = [
            [payload.get(name) for name in names]
            for payload in cached_table.rows
        ]
        table = Table(rows=rows, names=names)
        table.meta = {
            key: value
            for key, value in cached_table.metadata.items()
            if key != "columns"
        }
        table.meta.setdefault("name", cached_table.name)
        table.meta.setdefault("description", cached_table.description)
        for column_meta in column_metadata:
            name = str(column_meta.get("name", ""))
            if name not in table.colnames:
                continue
            column = table[name]
            column.description = column_meta.get("description")
            if column_meta.get("unit") is not None:
                column.unit = column_meta["unit"]
            if column_meta.get("ucd") is not None:
                column.meta["ucd"] = column_meta["ucd"]
        tables.append(table)
    return tables


def _star_identifier(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_identifier(value)
    match = re.fullmatch(r"(HD|HIP|GL|GJ|LHS)\s*0*(\d+)", normalized)
    return f"{match.group(1)} {int(match.group(2))}" if match else normalized


class ReferenceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{self.path}", future=True)
        ReferenceBase.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            columns = {
                row[1] for row in connection.exec_driver_sql(
                    "PRAGMA table_info(reference_rows)"
                )
            }
            if "stable_key" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE reference_rows ADD COLUMN stable_key VARCHAR(300)"
                )
            if "row_sha256" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE reference_rows ADD COLUMN row_sha256 VARCHAR(64)"
                )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_reference_rows_spatial "
                "ON reference_rows(table_id, dec_deg, ra_deg)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_reference_rows_stable_key "
                "ON reference_rows(table_id, stable_key)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_reference_alias_lookup "
                "ON reference_aliases(normalized_identifier, row_id)"
            )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self._backfill_aliases()
        self._materialize_current_documentation()

    def _materialize_current_documentation(self) -> None:
        """Restore companion ReadMes for reference DBs created before sidecars."""

        with self.sessions() as session:
            snapshots = list(session.scalars(
                select(ReferenceSnapshot).where(
                    ReferenceSnapshot.is_current.is_(True)
                )
            ))
            for snapshot in snapshots:
                tables = list(session.execute(
                    select(ReferenceTable.name, ReferenceTable.row_count)
                    .where(ReferenceTable.snapshot_id == snapshot.id)
                    .order_by(ReferenceTable.id)
                ))
                provider = (
                    "cds" if "/ftp/" in snapshot.source_url else "vizier"
                )
                materialize_catalog_documentation(
                    self.path,
                    provider=provider,
                    catalog_id=snapshot.catalog,
                    release=snapshot.catalog,
                    content_sha256=snapshot.content_sha256,
                    source_url=snapshot.source_url,
                    readme=snapshot.readme,
                    tables=tables,
                )

    def _backfill_aliases(self) -> None:
        """Populate the alias index for snapshots created before it existed."""
        with self.sessions() as session, session.begin():
            snapshots = list(session.scalars(select(ReferenceSnapshot)))
            for snapshot in snapshots:
                definition = SNAPSHOT_CATALOGS.get(snapshot.adapter)
                if definition is None:
                    continue
                tables = list(session.scalars(select(ReferenceTable).where(
                    ReferenceTable.snapshot_id == snapshot.id,
                    ReferenceTable.name.in_(definition.tables_for_matching),
                )))
                if not tables:
                    continue
                rows = session.scalars(
                    select(ReferenceRow)
                    .outerjoin(ReferenceAlias, ReferenceAlias.row_id == ReferenceRow.id)
                    .where(
                        ReferenceRow.table_id.in_([table.id for table in tables]),
                        ReferenceAlias.id.is_(None),
                    )
                )
                for row in rows:
                    payload = json.loads(row.payload_json)
                    aliases = {
                        _star_identifier(value)
                        for value in definition.lookup_identifiers(payload)
                    }
                    for alias in sorted(value for value in aliases if value):
                        session.add(ReferenceAlias(
                            row_id=row.id,
                            normalized_identifier=alias,
                        ))

    def fetch(
        self,
        adapter: str,
        client: SnapshotClient | None = None,
        *,
        cache_path: str | Path | None = None,
        refresh_cache: bool = False,
    ) -> SnapshotResult:
        definition = SNAPSHOT_CATALOGS[adapter]
        client = client or (
            CdsBulkSnapshotClient()
            if definition.catalog in CdsBulkSnapshotClient.files
            else AstroquerySnapshotClient()
        )
        provider = getattr(client, "provider", "vizier")
        try:
            if cache_path is None:
                tables = client.fetch_tables(definition.catalog)
                readme = client.fetch_readme(definition.catalog)
                source_url = (
                    client.source_url(definition.catalog)
                    if hasattr(client, "source_url")
                    else f"https://vizier.cds.unistra.fr/viz-bin/VizieR?-source={definition.catalog}"
                )
            else:
                cache = SnapshotCache(cache_path)
                cached_snapshot = None if refresh_cache else cache.current_snapshot(
                    provider, definition.catalog
                )
                if cached_snapshot is None:
                    fetched_tables = client.fetch_tables(definition.catalog)
                    fetched_readme = client.fetch_readme(definition.catalog)
                    if not fetched_tables:
                        raise ProviderError(
                            f"{adapter} snapshot returned no tables"
                        )
                    fetched_source_url = (
                        client.source_url(definition.catalog)
                        if hasattr(client, "source_url")
                        else f"https://vizier.cds.unistra.fr/viz-bin/VizieR?-source={definition.catalog}"
                    )
                    cached_snapshot = cache.store_snapshot(
                        provider=provider,
                        catalog_id=definition.catalog,
                        release=definition.catalog,
                        source_url=fetched_source_url,
                        readme=fetched_readme,
                        tables=fetched_tables,
                        note=f"reference adapter {definition.adapter}",
                    )
                tables = _cached_tables_as_astropy(cached_snapshot)
                readme = cached_snapshot.readme
                source_url = cached_snapshot.source_url
        except Exception as error:
            raise ProviderError(
                f"{adapter} snapshot fetch failed: {error}", transient=True
            ) from error
        if not tables:
            raise ProviderError(f"{adapter} snapshot returned no tables")
        if not readme.strip():
            raise ProviderError(f"{adapter} snapshot returned an empty ReadMe")

        serialized = []
        for table in tables:
            table_name = table.meta.get("name") or table.meta.get("ID")
            columns = [{
                "name": column.name,
                "datatype": str(column.dtype),
                "unit": None if column.unit is None else str(column.unit),
                "description": column.description,
                "meta": column.meta,
            } for column in table.itercols()]
            rows = [row_payload(row) for row in table]
            serialized.append({
                "name": table_name,
                "description": table.meta.get("description", ""),
                "meta": dict(table.meta),
                "columns": columns,
                "rows": rows,
            })
        canonical = _safe_json({
            "catalog": definition.catalog, "readme": readme, "tables": serialized
        })
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        materialize_catalog_documentation(
            self.path,
            provider=provider,
            catalog_id=definition.catalog,
            release=definition.catalog,
            content_sha256=digest,
            source_url=source_url,
            readme=readme,
            tables=(
                (str(item["name"]), len(item["rows"]))
                for item in serialized
            ),
        )
        with self.sessions() as session, session.begin():
            existing = session.scalar(select(ReferenceSnapshot).where(
                ReferenceSnapshot.catalog == definition.catalog,
                ReferenceSnapshot.content_sha256 == digest,
            ))
            if existing is not None:
                session.execute(update(ReferenceSnapshot).where(
                    ReferenceSnapshot.catalog == definition.catalog,
                    ReferenceSnapshot.is_current.is_(True),
                    ReferenceSnapshot.id != existing.id,
                ).values(is_current=False))
                existing.is_current = True
                count = sum(item["row_count"] for item in self.describe(existing.id))
                return SnapshotResult(
                    existing.id, definition.catalog, digest, len(serialized), count, True
                )
            snapshot = ReferenceSnapshot(
                adapter=definition.adapter,
                catalog=definition.catalog,
                content_sha256=digest,
                source_url=source_url,
                readme=readme,
                is_current=True,
            )
            session.execute(update(ReferenceSnapshot).where(
                ReferenceSnapshot.catalog == definition.catalog,
                ReferenceSnapshot.is_current.is_(True),
            ).values(is_current=False))
            session.add(snapshot)
            session.flush()
            total = 0
            for item in serialized:
                stored_table = ReferenceTable(
                    snapshot_id=snapshot.id,
                    name=item["name"],
                    description=item["description"],
                    metadata_json=_safe_json(item["meta"]),
                    row_count=len(item["rows"]),
                )
                session.add(stored_table)
                session.flush()
                session.add_all([ReferenceColumn(
                        table_id=stored_table.id,
                        position=position,
                        name=column["name"],
                        datatype=column["datatype"],
                        unit=column["unit"],
                        ucd=column["meta"].get("ucd"),
                        description=column["description"],
                        metadata_json=_safe_json(column["meta"]),
                    ) for position, column in enumerate(item["columns"])])
                session.flush()
                pending: list[tuple[ReferenceRow, tuple[str, ...]]] = []

                def flush_pending() -> None:
                    if not pending:
                        return
                    session.add_all([row for row, _ in pending])
                    session.flush()
                    session.add_all([
                        ReferenceAlias(row_id=row.id, normalized_identifier=alias)
                        for row, aliases in pending for alias in aliases
                    ])
                    session.flush()
                    pending.clear()

                for position, payload in enumerate(item["rows"], start=1):
                    name = row_text(payload, definition.primary_identifier)
                    if item["name"] in definition.tables_for_matching:
                        ra_deg, dec_deg = definition.position(payload)
                    else:
                        ra_deg, dec_deg = None, None
                    stored_row = ReferenceRow(
                        table_id=stored_table.id,
                        row_number=position,
                        source_identifier=name,
                        normalized_identifier=_star_identifier(name),
                        ra_deg=ra_deg,
                        dec_deg=dec_deg,
                        payload_json=_safe_json(payload),
                        stable_key=definition.row_key(payload, f"row:{position}"),
                        row_sha256=hashlib.sha256(
                            _safe_json(payload).encode()
                        ).hexdigest(),
                    )
                    aliases: tuple[str, ...] = ()
                    if item["name"] in definition.tables_for_matching:
                        alias_values = {
                            _star_identifier(value)
                            for value in definition.lookup_identifiers(payload)
                        }
                        aliases = tuple(sorted(
                            value for value in alias_values if value
                        ))
                    pending.append((stored_row, aliases))
                    if len(pending) >= 1000:
                        flush_pending()
                flush_pending()
                total += len(item["rows"])
            for relationship in definition.relationships:
                session.add(ReferenceRelationship(
                    snapshot_id=snapshot.id,
                    from_table=relationship.from_table,
                    from_column=relationship.from_column,
                    to_table=relationship.to_table,
                    to_column=relationship.to_column,
                    parser=relationship.parser,
                    description=relationship.description,
                ))
            return SnapshotResult(
                snapshot.id, definition.catalog, digest, len(serialized), total, False
            )

    def fetch_gaspar(self, client: SnapshotClient | None = None) -> SnapshotResult:
        return self.fetch("gaspar13", client)

    def fetch_v70a(self, client: SnapshotClient | None = None) -> SnapshotResult:
        return self.fetch("v70a", client)

    def current_snapshot(self, adapter: str) -> ReferenceSnapshot | None:
        with self.sessions() as session:
            return session.scalar(select(ReferenceSnapshot).where(
                ReferenceSnapshot.adapter == adapter,
                ReferenceSnapshot.is_current.is_(True),
            ))

    def backfill_positions(self, adapter: str) -> int:
        definition = SNAPSHOT_CATALOGS[adapter]
        with self.sessions() as session:
            snapshot = session.scalar(select(ReferenceSnapshot).where(
                ReferenceSnapshot.adapter == adapter,
                ReferenceSnapshot.is_current.is_(True),
            ))
            if snapshot is None:
                return 0
            tables = list(session.scalars(select(ReferenceTable).where(
                ReferenceTable.snapshot_id == snapshot.id,
                ReferenceTable.name.in_(definition.tables_for_matching),
            )))
            if not tables:
                return 0
            rows = session.execute(select(
                ReferenceRow.id, ReferenceRow.payload_json
            ).where(
                ReferenceRow.table_id.in_([table.id for table in tables]),
                or_(ReferenceRow.ra_deg.is_(None), ReferenceRow.dec_deg.is_(None)),
            )).yield_per(1000)
            statement = update(ReferenceRow).where(
                ReferenceRow.id == bindparam("row_id")
            ).values(
                ra_deg=bindparam("new_ra_deg"),
                dec_deg=bindparam("new_dec_deg"),
            )
            updates = []
            count = 0
            for row_id, payload_json in rows:
                ra_deg, dec_deg = definition.position(json.loads(payload_json))
                if ra_deg is None or dec_deg is None:
                    continue
                updates.append({
                    "row_id": row_id,
                    "new_ra_deg": ra_deg,
                    "new_dec_deg": dec_deg,
                })
                if len(updates) >= 1000:
                    session.connection().execute(statement, updates)
                    count += len(updates)
                    updates.clear()
            if updates:
                session.connection().execute(statement, updates)
                count += len(updates)
            session.commit()
            return count

    def describe(
        self, snapshot_id: int | None = None, *, adapter: str = "gaspar13"
    ) -> list[dict[str, object]]:
        with self.sessions() as session:
            if snapshot_id is None:
                snapshot = session.scalar(select(ReferenceSnapshot).where(
                    ReferenceSnapshot.adapter == adapter,
                    ReferenceSnapshot.is_current.is_(True),
                ))
                if snapshot is None:
                    return []
                snapshot_id = snapshot.id
            result = []
            tables = session.scalars(select(ReferenceTable).where(
                ReferenceTable.snapshot_id == snapshot_id
            ).order_by(ReferenceTable.id))
            for table in tables:
                columns = list(session.scalars(select(ReferenceColumn).where(
                    ReferenceColumn.table_id == table.id
                ).order_by(ReferenceColumn.position)))
                result.append({
                    "name": table.name,
                    "description": table.description,
                    "row_count": table.row_count,
                    "columns": [{
                        "name": column.name,
                        "datatype": column.datatype,
                        "unit": column.unit,
                        "ucd": column.ucd,
                        "description": column.description,
                    } for column in columns],
                })
            return result

    def relationships(
        self, snapshot_id: int | None = None, *, adapter: str = "gaspar13"
    ) -> list[ReferenceRelationship]:
        with self.sessions() as session:
            if snapshot_id is None:
                snapshot = session.scalar(select(ReferenceSnapshot).where(
                    ReferenceSnapshot.adapter == adapter,
                    ReferenceSnapshot.is_current.is_(True),
                ))
                if snapshot is None:
                    return []
                snapshot_id = snapshot.id
            return list(session.scalars(select(ReferenceRelationship).where(
                ReferenceRelationship.snapshot_id == snapshot_id
            )))

    def rows(
        self,
        table_name: str,
        snapshot_id: int | None = None,
        *,
        adapter: str = "gaspar13",
    ) -> list[dict[str, object]]:
        with self.sessions() as session:
            if snapshot_id is None:
                snapshot = session.scalar(select(ReferenceSnapshot).where(
                    ReferenceSnapshot.adapter == adapter,
                    ReferenceSnapshot.is_current.is_(True),
                ))
                if snapshot is None:
                    return []
                snapshot_id = snapshot.id
            table = session.scalar(select(ReferenceTable).where(
                ReferenceTable.snapshot_id == snapshot_id,
                (ReferenceTable.name == table_name)
                | ReferenceTable.name.endswith(f"/{table_name}"),
            ))
            if table is None:
                return []
            return [
                json.loads(row.payload_json)
                for row in session.scalars(select(ReferenceRow).where(
                    ReferenceRow.table_id == table.id
                ).order_by(ReferenceRow.row_number))
            ]

    def snapshot_by_hash(self, adapter: str, digest: str) -> ReferenceSnapshot | None:
        with self.sessions() as session:
            return session.scalar(select(ReferenceSnapshot).where(
                ReferenceSnapshot.adapter == adapter,
                ReferenceSnapshot.content_sha256 == digest,
            ))

    def row_hashes(
        self,
        snapshot_id: int,
        table_name: str | tuple[str, ...] = "table2",
        key_columns: tuple[str, ...] = ("Name",),
    ) -> dict[str, str]:
        with self.sessions() as session:
            table_names = (table_name,) if isinstance(table_name, str) else table_name
            exact = ReferenceTable.name.in_(table_names)
            suffixes = [
                ReferenceTable.name.endswith(f"/{name}") for name in table_names
            ]
            clause = or_(exact, *suffixes)
            tables = list(session.scalars(select(ReferenceTable).where(
                ReferenceTable.snapshot_id == snapshot_id, clause,
            )))
            if not tables:
                return {}
            table_ids = [table.id for table in tables]
            missing = session.execute(select(
                ReferenceRow.id,
                ReferenceRow.row_number,
                ReferenceRow.payload_json,
            ).where(
                ReferenceRow.table_id.in_(table_ids),
                or_(
                    ReferenceRow.stable_key.is_(None),
                    ReferenceRow.row_sha256.is_(None),
                ),
            )).yield_per(1000)
            updates = []
            statement = update(ReferenceRow).where(
                ReferenceRow.id == bindparam("row_id")
            ).values(
                stable_key=bindparam("new_stable_key"),
                row_sha256=bindparam("new_row_sha256"),
            )
            for row_id, row_number, payload_json in missing:
                payload = json.loads(payload_json)
                first = row_text(payload, key_columns[0]) if key_columns else None
                key = first or f"row:{row_number}"
                for column in key_columns[1:]:
                    value = row_text(payload, column)
                    if value:
                        key += f"|{column}={value}"
                updates.append({
                    "row_id": row_id,
                    "new_stable_key": key,
                    "new_row_sha256": hashlib.sha256(payload_json.encode()).hexdigest(),
                })
                if len(updates) >= 1000:
                    session.connection().execute(statement, updates)
                    updates.clear()
            if updates:
                session.connection().execute(statement, updates)
            session.commit()
            return dict(session.execute(select(
                ReferenceRow.stable_key, ReferenceRow.row_sha256
            ).where(ReferenceRow.table_id.in_(table_ids))).tuples().all())
