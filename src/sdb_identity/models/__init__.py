"""SQLAlchemy model registry. Importing this package registers all tables."""

from .base import Base
from . import identity, samples, alma, catalogs, metadata, hierarchy, photometry, batch, curated, reference, exports  # noqa: F401

__all__ = ["Base"]
