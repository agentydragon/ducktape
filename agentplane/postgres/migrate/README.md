# Agentplane database migrations

Every PostgreSQL-backed service embeds its Goose migrations in the serving binary. Normal
startup calls `Run(ctx, databaseURL, migrations.Up)` before starting application workers or
handling requests. The context is cancelled by SIGINT/SIGTERM. Migration failure exits startup;
the process never serves against a partially initialized schema.

`Run` retries initial connections for at most two minutes, then allows two minutes for migration,
including advisory-lock waiting. Migration connections have a five-second DDL lock timeout, so
schema changes fail instead of indefinitely blocking serving writers. Failed SQL is not blindly
replayed inside `Run`; Goose transactions/history and any registered Go migration define what
the next startup resumes. Log connection failures, migration start/completion and returned errors.

Each service owns embedded migration files, a stable Goose history table and a stable advisory
lock key. Use Goose's `Provider` with its PostgreSQL session locker and disabled global registry.
Every replica may initialize; the lock serializes migrators. It does not stop application writers.
Release migration resources before starting the runtime's pools, leases, workers and listeners.

Services use rolling updates. Schema changes must support old and new writers overlapping, or
provide an explicit writer cutover for the incompatible change. A startup probe allows five
minutes for initialization, covering both shared budgets and ordinary startup. Readiness stays
false until the service can handle requests. An init container or completed migration Job is not
the schema authority; the database's own Goose history is.

The same binary accepts `--migrate-only`, with the service's database URL environment variable.
Call `RunOnly` using the signal-aware startup context before loading serving-only settings. This
mode uses the identical `Run` and migration provider, then exits; fixtures can prepare database
templates without starting a service. It is not an extra deployment step or separate image.

## Schema changes and baselines

- Add changes as new versioned Goose SQL files or explicitly registered Go migrations. Applied
  migrations are immutable unless the operator explicitly selects a squash. No unversioned
  startup data converters or recurring validation of a retired migration framework.
- A selected squashed baseline creates the current schema on a fresh empty database and adopts
  explicitly recognized deployed predecessors. Validate the required version and schema before
  adoption; preserve authored rows, identities, evidence and existing Goose history. Unknown or
  partial predecessors fail with an actionable error instead of guessing how to repair them.
- `CREATE TABLE IF NOT EXISTS` does not validate an existing table's shape. Baseline adoption
  checks are one-time migration work, not a second permanent schema model in startup code.
- Use transactional changes where possible. A Go backfill with several transactions must make
  interrupted progress safe to resume; store any required progress durably. Completed transforms
  are skipped by Goose history on later startups.
- Reverting a service binary does not imply downgrading its database. Use a compatible older
  binary or an explicitly reviewed forward repair/restore procedure; do not claim a comment-only
  `Down` block reverses retained state.

MCP edge's cross-database Connection/OAuth transfer is a separate ownership handoff with explicit
source/destination writer fencing. It is not ordinary schema initialization. Runner's per-path
SQLite journal applies its SQLite Goose migrations when opened, before journal recovery; it
does not use PostgreSQL advisory locks or a migration container.
