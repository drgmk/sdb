"""Dependencies shared by the local review HTTP route groups."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from .service import IdentityService


@dataclass(frozen=True)
class ReviewWebContext:
    session_factory: sessionmaker[Session]
    sample: str | None = None
    identity_service_factory: Callable[[], IdentityService] | None = None
    catalog_service_factory: Callable[[str, str], object] | None = None
    catalog_coverage_providers: tuple[str, ...] | None = None
    catalog_update_factory: Callable[[], object] | None = None
    reference_store: object | None = None
