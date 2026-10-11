# Console schema baseline

`0135` is the frozen final schema of the former chain, retaining its last revision
ID. Databases already at `0135` apply nothing. An older deployment must reach
`0135` using the pre-squash image before adopting this baseline; do not stamp an
older database forward or reset it. Downgrade-to-base remains unsupported, as it
was for the previous console baseline.

The baseline uses explicit Alembic/SQLAlchemy definitions to create the surviving tables, types, functions, triggers,
constraints and indexes, including their physical legacy names. It preserves the
`haku-state` logical index registration, without resurrecting the removed chat
registration. The `vector` extension remains an external provisioning prerequisite
(CNPG in deployment, the fixture in tests), not a new superuser migration operation.

## Equivalence evidence

Dumps are independent verification artifacts, not executable migration code.

The original chain at `b78b07ffce` ran in disposable PostgreSQL CI:

- BuildBuddy invocation `f6cf5575-0d05-56d2-9cd4-868341e766f4`.
- Target `//haku/console:test_schema_baseline`.
- Artifact `test.outputs/haku_console_schema.sql`.

That capture verified `0135` when it landed; later migrations are ordinary children. Current-schema
tests: `test_agent_authority_schema.py` (ORM, row preservation) and `test_schema_baseline.py`
(upgrade-at-head leaves the schema and object OIDs untouched).
