# Study Casino TODO

## After Postgres migration — possible refactors

These are not required, just nice-to-haves surfaced during the
2026-05-16 SQLite→Postgres cutover and the server-authoritative casino cutover.
Defer until there's a real need.

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

- Database backup/restore work is tracked in <../cluster/k8s/TODO.md>.
