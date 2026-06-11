# Study Casino TODO

## After Postgres migration — possible refactors

These are not required, just nice-to-haves surfaced during the
2026-05-16 SQLite→Postgres cutover. Defer until there's a real need.

- [ ] Push `username` into the `ServerActionMutator` signature instead of
      passing via closure. Today every endpoint's mutator captures `username`
      from the enclosing scope; making it an explicit parameter of the
      mutator callable would surface the user-scoping dependency at every
      ORM-row construction and helper invocation.
- [ ] Replace the SELECT-then-INSERT lazy-seed in `SqlStore._ensure_user`
      with a dialect-aware upsert (`INSERT ... ON CONFLICT DO NOTHING`)
      so first-touch by two concurrent requests can't race. Current
      behavior is fine for a single-replica deployment.
- [ ] Reconsider whether the casino actually needs multi-tenancy. The
      shared-schema refactor was driven by symmetry with other CNPG apps
      in the cluster; if auragon stays the only user, the `user_id`
      column adds friction without value and could be dropped.
- [ ] The CNPG cluster is provisioned with `instances: 2` (VPS-HA) but
      the app deployment is still `replicas: 1`. The DB can survive a
      single VPS loss; the app cannot. Decide whether to bump the app
      to `replicas: 2` with proper Postgres-backed session state, or
      accept the asymmetry.
- [ ] Consider migrating the `*_at_ms` columns from `BigInteger` (Unix
      milliseconds) to Postgres `TIMESTAMP WITH TIME ZONE`. Today the
      columns are bigints because the wire format (`/state` JSON,
      frontend) uses ms-since-epoch integers, and changing the column
      type would force either a JSON schema change or a model-layer
      adapter (datetime in the DB, int on the wire). Defer until there's
      a real reason — e.g. needing time-range queries that bigint
      indexes don't serve well.

## Notes

- Pre-cutover rows in `game_events` (`source="client_reported"`) and
  `ledger_events` (`source="legacy_client_sync"`) stay readable forever; the
  Literal unions in `events.py` keep both source values so old rows
  deserialize. Do not write a migration that rewrites them.
