# Agentplane database migrations

PostgreSQL services run Goose from a migration init container before that Pod's
serving container starts. The shared [`Run`](migrate.go) helper retries the
initial database connection; each service owns its migration files, Goose
version table, and fixed advisory lock key. A failed migration blocks that Pod.
Other replicas may still serve during a rolling update. The Goose lock
serializes migrators, **not** application writers or independently pinned
serving and migration images. Plan schema changes to work with any old writers
that can remain live, or explicitly stop those writers for a cutover.

- Add schema changes as new, versioned Goose SQL files or registered Go
  migrations. Do not edit an applied migration or run an unversioned startup
  converter. A repeat startup must not replay a completed data transformation;
  Goose history, not a process-local flag, records that boundary.
- Keep each database's Goose history table and advisory lock key stable. In
  particular, preserve a legacy migrator's lock key during a handoff. Run a
  migration only in the owning service's init container; the serving process
  does not migrate its PostgreSQL database.
- A new baseline may create the schema on an empty database and adopt an
  explicitly recognized deployed schema. Validate the old version and shape
  before recording adoption. Incomplete or unknown states must fail without
  rewriting retained rows. Do not infer that `CREATE TABLE IF NOT EXISTS`
  validates an existing table's shape.
- Migration locks do not replace a stop-writer cutover. Actions and
  Notification use Recreate to stop old writers during migrations. MCP edge's
  Connection/OAuth transfer is a separate, operator-run data handoff with its
  own source and destination fences; it is not a Goose migration or an
  automatic startup step.

The app currently holds its advisory lock across a read-only Alembic/schema
preflight **before** Goose can create metadata. Any required pre-metadata
validation must stay serialized with migration. History's bounded event-kind
backfill is registered as Goose migration 4; non-SQL transformations belong in
versioned Go migrations. The Runner's per-path SQLite journal is different:
`journal.Open` applies its SQLite Goose migrations before recovering journal
entries; it has no PostgreSQL init container or advisory key.
