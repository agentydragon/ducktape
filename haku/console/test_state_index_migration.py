"""The index's two schema definitions must not drift.

`haku/state_index/schema.py` is what the code queries and what `store.ensure_schema` builds for
the CLI and the tests; migration 0037 is what the deployed database gets. Nothing else compares
them, and a column added to one and not the other would pass every other test in the repo and
fail in production at the first query.
"""

from __future__ import annotations

import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, make_url

from haku.console.database_migrate import apply_migrations
from haku.state_index.schema import SCHEMA, Base


def _only_the_index_schema(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Keep the comparison to `state_index`.

    `include_schemas` is what makes Alembic look outside `public` at all, and it looks at *every*
    schema — so without this the console's own tables all read as "not in this metadata" and the
    comparison is a list of everything else the database contains.
    """
    return name == SCHEMA if type_ == "schema" else True


def test_the_migration_builds_exactly_what_the_orm_declares(db_url: str) -> None:
    apply_migrations(db_url)
    # The fixture hands out an asyncpg URL for the app; comparison is synchronous.
    engine = create_engine(make_url(db_url).set(drivername="postgresql+psycopg").render_as_string(False))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "include_schemas": True, "include_name": _only_the_index_schema}
            )
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
