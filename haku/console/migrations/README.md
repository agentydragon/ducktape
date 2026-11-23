# Console schema baseline

Revision `0138` creates the current schema on a fresh PostgreSQL database. A database already
at `0138` applies no DDL or data updates. Before restoring an older backup with this image,
use the pre-squash image to upgrade it to `0138`, including revocation of static Agent
credentials. Never stamp an older database forward. Downgrade-to-base remains unsupported.

## Equivalence evidence

Dumps are independent verification artifacts, not executable migration code.

The original chain at `b78b07ffce` ran in disposable PostgreSQL CI:

- BuildBuddy invocation `f6cf5575-0d05-56d2-9cd4-868341e766f4`.
- Historical target `//haku/console:test_schema_baseline` (now consolidated into
  `//haku/console:test_agent_authority_schema`).
- Artifact `test.outputs/haku_console_schema.sql`.

That capture verified the former baseline, `0135`, when it landed. Current-schema
checks live in `test_agent_authority_schema.py`: ORM agreement, row preservation, enum values, and
head reapplication preserving the schema dump, public relation OIDs, migration stamp and seeded row.
