"""Drop the source-specific dirty-target tables now unified into export_dirty_targets.

`dataset_dirty_targets`, `reference_dirty_targets`, and `catalog_dirty_targets`
were redundant with `export_dirty_targets` (the single load-bearing dirty table,
fed by `mark_export_dirty` since migration 0014). Their writers now route solely
through `mark_export_dirty`, and the `.pending()` readers query
`export_dirty_targets` filtered by `source_type`. This migration drops the three
tables and their unused `pending_*_exports` views.
"""

from alembic import op
from sqlalchemy import inspect

revision = "0040_drop_source_dirty_tables"
down_revision = "0039_drop_hierarchy_edge_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP VIEW IF EXISTS pending_dataset_exports")
    op.execute("DROP VIEW IF EXISTS pending_reference_exports")
    op.execute("DROP VIEW IF EXISTS pending_catalog_exports")
    tables = set(inspect(op.get_bind()).get_table_names())
    for name in ("dataset_dirty_targets", "reference_dirty_targets", "catalog_dirty_targets"):
        if name in tables:
            op.drop_table(name)


def downgrade():
    raise NotImplementedError(
        "0040 consolidates the source-specific dirty tables into export_dirty_targets "
        "and cannot be reversed"
    )
