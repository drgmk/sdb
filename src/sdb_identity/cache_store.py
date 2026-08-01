from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .serialization import row_payload as _row_payload, safe_json as _safe_json
from .catalogs.provenance import materialize_catalog_documentation


def utcnow():
    return datetime.now(timezone.utc)


class CacheBase(DeclarativeBase):
    pass


class CachedSource(CacheBase):
    __tablename__ = "cached_sources"
    __table_args__ = (UniqueConstraint("provider", "catalog_id", "content_sha256"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    catalog_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    release: Mapped[str] = mapped_column(String(200), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    readme: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class CachedTable(CacheBase):
    __tablename__ = "cached_tables"
    __table_args__ = (UniqueConstraint("source_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("cached_sources.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)


class CachedRow(CacheBase):
    __tablename__ = "cached_rows"
    __table_args__ = (UniqueConstraint("table_id", "row_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("cached_tables.id"), nullable=False, index=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    row_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


@dataclass(frozen=True)
class CachedTableData:
    name: str
    description: str
    metadata: dict[str, object]
    rows: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class CachedSnapshotData:
    source_id: int
    provider: str
    catalog_id: str
    release: str
    content_sha256: str
    source_url: str
    readme: str
    unchanged: bool
    tables: tuple[CachedTableData, ...]


@dataclass(frozen=True)
class CachedSnapshotSummary:
    source_id: int
    provider: str
    catalog_id: str
    release: str
    content_sha256: str
    source_url: str
    fetched_at: str
    is_current: bool
    note: str | None
    table_count: int
    row_count: int


@dataclass(frozen=True)
class CacheValidationResult:
    ok: bool
    provider: str
    catalog_id: str
    source_id: int
    content_sha256: str
    table_count: int
    row_count: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def _table_name(table, fallback: str) -> str:
    meta = getattr(table, "meta", {}) or {}
    return str(meta.get("name") or meta.get("ID") or fallback)


def _table_description(table) -> str:
    meta = getattr(table, "meta", {}) or {}
    return str(meta.get("description") or meta.get("Description") or "")


def _table_metadata(table) -> dict[str, object]:
    meta = dict(getattr(table, "meta", {}) or {})
    columns = []
    for index, name in enumerate(getattr(table, "colnames", ())):
        column = table[name]
        columns.append({
            "position": index,
            "name": str(name),
            "dtype": str(getattr(column, "dtype", "")),
            "unit": None if getattr(column, "unit", None) is None else str(column.unit),
            "description": getattr(column, "description", None),
            "ucd": getattr(column, "ucd", None) or getattr(column, "meta", {}).get("ucd"),
        })
    meta["columns"] = columns
    return meta


def _table_rows(table) -> tuple[dict[str, object], ...]:
    return tuple(_row_payload(row) for row in table)


class SnapshotCache:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{self.path}", future=True)
        CacheBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self._materialize_current_documentation()

    def _materialize_current_documentation(self) -> None:
        """Restore companion ReadMes for caches created before sidecars."""

        with self.sessions() as session:
            sources = list(session.scalars(
                select(CachedSource).where(CachedSource.is_current.is_(True))
            ))
            for source in sources:
                tables = list(session.execute(
                    select(CachedTable.name, CachedTable.row_count)
                    .where(CachedTable.source_id == source.id)
                    .order_by(CachedTable.id)
                ))
                materialize_catalog_documentation(
                    self.path,
                    provider=source.provider,
                    catalog_id=source.catalog_id,
                    release=source.release,
                    content_sha256=source.content_sha256,
                    source_url=source.source_url,
                    readme=source.readme,
                    tables=tables,
                )

    def current_snapshot(
        self, provider: str, catalog_id: str
    ) -> CachedSnapshotData | None:
        provider = provider.lower().strip()
        catalog_id = catalog_id.strip()
        with self.sessions() as session:
            source = session.scalar(
                select(CachedSource)
                .where(CachedSource.provider == provider)
                .where(CachedSource.catalog_id == catalog_id)
                .where(CachedSource.is_current.is_(True))
                .order_by(CachedSource.id.desc())
            )
            if source is None:
                return None
            return self._snapshot_data(session, source, unchanged=True)

    def current_snapshot_for_catalog(
        self, catalog_id: str, *, provider: str | None = None
    ) -> CachedSnapshotData | None:
        catalog_id = catalog_id.strip()
        provider = provider.lower().strip() if provider else None
        with self.sessions() as session:
            statement = (
                select(CachedSource)
                .where(CachedSource.catalog_id == catalog_id)
                .where(CachedSource.is_current.is_(True))
                .order_by(CachedSource.id.desc())
            )
            if provider is not None:
                statement = statement.where(CachedSource.provider == provider)
            sources = list(session.scalars(statement))
            if not sources:
                return None
            if len(sources) > 1 and provider is None:
                providers = ", ".join(sorted(source.provider for source in sources))
                raise KeyError(
                    f"catalog {catalog_id!r} has multiple providers: {providers}; "
                    "specify --provider"
                )
            return self._snapshot_data(session, sources[0], unchanged=True)

    def summaries(self, *, include_old: bool = False) -> list[CachedSnapshotSummary]:
        with self.sessions() as session:
            statement = select(CachedSource).order_by(
                CachedSource.provider, CachedSource.catalog_id, CachedSource.id.desc()
            )
            if not include_old:
                statement = statement.where(CachedSource.is_current.is_(True))
            result = []
            for source in session.scalars(statement):
                table_count, row_count = session.execute(
                    select(
                        func.count(CachedTable.id),
                        func.coalesce(func.sum(CachedTable.row_count), 0),
                    ).where(CachedTable.source_id == source.id)
                ).one()
                result.append(CachedSnapshotSummary(
                    source_id=source.id,
                    provider=source.provider,
                    catalog_id=source.catalog_id,
                    release=source.release,
                    content_sha256=source.content_sha256,
                    source_url=source.source_url,
                    fetched_at=source.fetched_at.isoformat(),
                    is_current=source.is_current,
                    note=source.note,
                    table_count=int(table_count or 0),
                    row_count=int(row_count or 0),
                ))
            return result

    def validate(
        self, catalog_id: str, *, provider: str | None = None
    ) -> CacheValidationResult:
        snapshot = self.current_snapshot_for_catalog(catalog_id, provider=provider)
        if snapshot is None:
            raise KeyError(f"cached snapshot not found: {catalog_id}")
        errors: list[str] = []
        warnings: list[str] = []
        if not snapshot.source_url.strip():
            errors.append("missing source URL")
        if not snapshot.readme.strip():
            errors.append("missing ReadMe")
        if not snapshot.content_sha256.strip():
            errors.append("missing content checksum")
        if not snapshot.tables:
            errors.append("snapshot has no tables")
        row_count = 0
        for table in snapshot.tables:
            if not table.name.strip():
                errors.append("table with blank name")
            if not table.description.strip():
                warnings.append(f"{table.name}: missing description")
            if not table.rows:
                errors.append(f"{table.name}: no rows")
            row_count += len(table.rows)
            columns = table.metadata.get("columns")
            if not isinstance(columns, list) or not columns:
                errors.append(f"{table.name}: missing column metadata")
                continue
            names = [column.get("name") for column in columns if isinstance(column, dict)]
            if not all(isinstance(name, str) and name for name in names):
                errors.append(f"{table.name}: invalid column metadata")
            if table.rows:
                missing = [
                    name for name in names
                    if isinstance(name, str) and name not in table.rows[0]
                ]
                if missing:
                    warnings.append(
                        f"{table.name}: first row missing metadata columns "
                        f"{', '.join(missing[:5])}"
                    )
        return CacheValidationResult(
            ok=not errors,
            provider=snapshot.provider,
            catalog_id=snapshot.catalog_id,
            source_id=snapshot.source_id,
            content_sha256=snapshot.content_sha256,
            table_count=len(snapshot.tables),
            row_count=row_count,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    def store_snapshot(
        self,
        *,
        provider: str,
        catalog_id: str,
        release: str,
        source_url: str,
        readme: str,
        tables: Iterable[object],
        note: str | None = None,
    ) -> CachedSnapshotData:
        provider = provider.lower().strip()
        catalog_id = catalog_id.strip()
        table_data = tuple(
            CachedTableData(
                name=_table_name(table, f"{catalog_id}/table{index + 1}"),
                description=_table_description(table),
                metadata=_table_metadata(table),
                rows=_table_rows(table),
            )
            for index, table in enumerate(tables)
        )
        canonical = _safe_json({
            "provider": provider,
            "catalog_id": catalog_id,
            "readme": readme,
            "tables": [
                {
                    "name": table.name,
                    "description": table.description,
                    "metadata": table.metadata,
                    "rows": table.rows,
                }
                for table in table_data
            ],
        })
        content_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
        materialize_catalog_documentation(
            self.path,
            provider=provider,
            catalog_id=catalog_id,
            release=release,
            content_sha256=content_sha256,
            source_url=source_url,
            readme=readme,
            tables=((table.name, len(table.rows)) for table in table_data),
        )
        with self.sessions.begin() as session:
            existing = session.scalar(
                select(CachedSource)
                .where(CachedSource.provider == provider)
                .where(CachedSource.catalog_id == catalog_id)
                .where(CachedSource.content_sha256 == content_sha256)
            )
            if existing is not None:
                session.execute(
                    update(CachedSource)
                    .where(CachedSource.provider == provider)
                    .where(CachedSource.catalog_id == catalog_id)
                    .values(is_current=False)
                )
                existing.is_current = True
                return self._snapshot_data(session, existing, unchanged=True)

            session.execute(
                update(CachedSource)
                .where(CachedSource.provider == provider)
                .where(CachedSource.catalog_id == catalog_id)
                .values(is_current=False)
            )
            source = CachedSource(
                provider=provider,
                catalog_id=catalog_id,
                release=release,
                content_sha256=content_sha256,
                source_url=source_url,
                readme=readme,
                is_current=True,
                note=note,
            )
            session.add(source)
            session.flush()
            for table in table_data:
                table_row = CachedTable(
                    source_id=source.id,
                    name=table.name,
                    description=table.description,
                    metadata_json=_safe_json(table.metadata),
                    row_count=len(table.rows),
                )
                session.add(table_row)
                session.flush()
                for row_number, payload in enumerate(table.rows, start=1):
                    payload_json = _safe_json(payload)
                    session.add(CachedRow(
                        table_id=table_row.id,
                        row_number=row_number,
                        payload_json=payload_json,
                        row_sha256=hashlib.sha256(payload_json.encode()).hexdigest(),
                    ))
            return CachedSnapshotData(
                source_id=source.id,
                provider=provider,
                catalog_id=catalog_id,
                release=release,
                content_sha256=content_sha256,
                source_url=source_url,
                readme=readme,
                unchanged=False,
                tables=table_data,
            )

    def _snapshot_data(
        self,
        session,
        source: CachedSource,
        *,
        unchanged: bool,
    ) -> CachedSnapshotData:
        tables = []
        for table in session.scalars(
            select(CachedTable)
            .where(CachedTable.source_id == source.id)
            .order_by(CachedTable.id)
        ):
            rows = tuple(
                json.loads(row.payload_json)
                for row in session.scalars(
                    select(CachedRow)
                    .where(CachedRow.table_id == table.id)
                    .order_by(CachedRow.row_number)
                )
            )
            tables.append(CachedTableData(
                name=table.name,
                description=table.description,
                metadata=json.loads(table.metadata_json),
                rows=rows,
            ))
        return CachedSnapshotData(
            source_id=source.id,
            provider=source.provider,
            catalog_id=source.catalog_id,
            release=source.release,
            content_sha256=source.content_sha256,
            source_url=source.source_url,
            readme=source.readme,
            unchanged=unchanged,
            tables=tuple(tables),
        )
