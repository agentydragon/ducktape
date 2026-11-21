# InvenTree TODO

> **Status: parked.** The manifests are parked under `cluster/parked/`. Revive the package before working on this backlog.

## Secrets if reviving

- [ ] Add SOPS secrets for InvenTree admin and database passwords before
      reviving it. The old Vault `kv/inventree/*` values are gone with
      Vault (decommissioned 2026-04-19; <../../docs/decisions.md> § "Secrets: SOPS
      SSOT"), so generate fresh values.

## Token provisioner if reviving

- [ ] Restore the [provisioner source and Bazel package](https://github.com/agentydragon/ducktape/tree/64d2b9114b4f53d1800fab7004efc5432d775f86/cluster/archive/inventree_token_provisioner)
      from Git. Restore CI image publishing and Flux's ImageRepository and ImagePolicy
      (removed in [#7050](https://github.com/agentydragon/ducktape/pull/7050)).
      Update the parked Job and CronJob to use the rebuilt image before unsuspending
      the token provisioner.
