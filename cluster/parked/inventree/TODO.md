# InvenTree TODO

> **Status: parked.** The manifests are parked under `cluster/parked/`. Revive the package before working on this backlog.

## Secrets if reviving

- [ ] Add SOPS secrets for InvenTree admin and database passwords before
      reviving it. The old Vault `kv/inventree/*` values are gone with
      Vault (decommissioned 2026-04-19; <../../docs/decisions.md> § "Secrets: SOPS
      SSOT"), so generate fresh values.
