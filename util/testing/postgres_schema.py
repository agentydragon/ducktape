"""Comparable snapshots of a PostgreSQL schema, for pinning one migration history to the schema of another."""

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from sqlalchemy import Engine, text

# Each query lists one kind of schema object, ordered by its columns, so two schemas compare as lists.
# Column order inside a table is not compared: no reader depends on it.
_CATALOG = {
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
              AND c.relname <> ALL(:migrator_tables)
        ORDER BY 1, 2""",
    # Without NOT NULL constraints (PostgreSQL 18 lists them): `columns` holds nullability, and a constraint
    # keeps the name of its column from before a rename, which a schema built afresh cannot reproduce.
    "constraints": """
        SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND con.contype <> 'n' AND c.relname <> ALL(:migrator_tables)
        ORDER BY 1, 2""",
    "indexes": """
        SELECT tablename, indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename <> ALL(:migrator_tables)
        ORDER BY 1, 2""",
    "sequences": """
        SELECT sequencename, data_type::text, start_value, min_value, max_value, increment_by, cycle
        FROM pg_sequences WHERE schemaname = 'public' AND sequencename NOT LIKE :migrator_sequences
        ORDER BY 1""",
    "replica_identity": """
        SELECT c.relname, c.relreplident FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> ALL(:migrator_tables)
        ORDER BY 1""",
}


def schema_catalog(
    engine: Engine,
    *,
    migrator_tables: Sequence[str],
    migrator_sequence_prefix: str,
    extra_queries: Mapping[str, str] = MappingProxyType({}),
) -> dict[str, list[tuple[object, ...]]]:
    """The `public` schema's tables, constraints, indexes, sequences and replica identities.

    `migrator_tables` and the sequences named `migrator_sequence_prefix`* are the migrators' own
    bookkeeping, which two histories do not share. `extra_queries` adds kinds of object a schema
    carries beyond those, each ordered like the built-in ones and taking no parameters.
    """
    parameters = {"migrator_tables": list(migrator_tables), "migrator_sequences": f"{migrator_sequence_prefix}%"}
    with engine.connect() as conn:
        return {
            **{kind: [tuple(row) for row in conn.execute(text(query), parameters)] for kind, query in _CATALOG.items()},
            **{kind: [tuple(row) for row in conn.execute(text(query))] for kind, query in extra_queries.items()},
        }
