# ActivityWatch

Personal activity tracking via
[aw-server-rust](https://github.com/ActivityWatch/aw-server-rust). One central aw-server
on the cluster is the query surface and the sole writer of its own store; each device
keeps local capture and runs a small importer that pushes into it over a bearer-gated
write route — no snapshot files, no `aw-sync`, no Syncthing.

**Revived 2026-08-26** on the repo-owned idempotent importer
(`@ducktape_activitywatch//importer`): each device reads its own aw-server over REST and
pushes into the central one at `https://activitywatch-write.allegedly.works`, deduped and
idempotent (a re-run inserts only genuinely new events). Routine runs use a bounded
one-hour overlap from the destination's newest event; `--full-reconcile` is an explicit
add-only recovery mode for older gaps. rugged and wyrm2 are verified feeding; iguana and
atlas are enabled and join on their next Home Manager switch. The remaining write-route
hardening is tracked in [revival-plan.md](revival-plan.md).

**History.** The previous transport — desktop `aw-sync` staging databases shipped through
Syncthing into a cluster receiver and imported by an `aw-sync` CronJob — was retired
2026-08-17: upstream `aw-sync` mutated its SQLite inputs, collapsed provenance, and was
not idempotent, so it could not produce a canonical central dataset. The old Authentik
human route and agent OAuth client-credential mint were removed with that deployment.
The human route is now restored as the Authentik proxy described below; agents use the
separate bearer-gated read route. Records from that era, kept for their lessons:

- [Importer canary](importer-canary.md): the live `aw-sync` pull canary that forced the
  retirement — source mutation, provenance collisions, heartbeat amplification.
- [Syncthing conflicts](syncthing-conflicts.md): receiver index-loss and conflict-file
  RCA.
- [GNOME Wayland capture](gnome-wayland-capture.md): why the stock watcher saw no windows
  on GNOME Wayland; fixed by `awatcher` + the focused-window-d-bus extension, now the
  deployed capture stack.

## Design

One pod (`cluster/k8s/activitywatch/`, namespace `activitywatch`, `Recreate`), pinned
to the Proxmox region next to its PVC: `aw-server` (`git.allegedly.works/ducktape-ci/aw-server`,
Bazel-built `@ducktape_activitywatch//:image`) with device id `activitywatch-cluster` and
SQLite at `/data/db.sqlite3` on `activitywatch-data` (`local-path-proxmox`, 10Gi), plus
two nginx sidecars that are its only auth — aw-server itself has none:

| Port | Container        | Auth              | Allowed                    | Reached via                                   |
| ---- | ---------------- | ----------------- | -------------------------- | --------------------------------------------- |
| 5600 | `aw-server`      | none              | everything                 | pod-local (probes + sidecars)                 |
| 5601 | `readonly-proxy` | Authentik fronted | GET + POST `/api/0/query/` | `activitywatch.allegedly.works` via Authentik |
| 5602 | `bearer-proxy`   | `AW_WRITE_TOKEN`  | all methods                | `activitywatch-write.allegedly.works`         |
| 5603 | `bearer-proxy`   | `AW_READ_TOKEN`   | GET + POST `/api/0/query/` | `activitywatch-read.allegedly.works`          |

The three public hostnames are HTTPRoutes on the shared `cluster-gateway`: the human UI is
Authentik-fronted, while the read and write API routes are bearer-gated. The
CiliumNetworkPolicy admits only kube-apiserver probes (5600), Authentik (5601), and the
Gateway (5602/5603), with DNS-only egress. Both tokens are SOPS Secrets next to the manifests: the write token
is dual-recipient (cluster-secrets + the desktops' user age keys), so one value serves
the proxy and every desktop's `AW_DEST_TOKEN`; the read token is also reflected into
`haku-egress-proxy` for the iron proxy (below). The write route currently allows GET
because the importer reads the destination to dedup, so a leaked write token can read
history too; it becomes write-only once the importer syncs incrementally
([revival-plan.md](revival-plan.md)). The image tag is pinned in `deployment.yaml` and
updated through the normal image-automation configuration.

**Device transport** (`nix/home/services/activitywatch.nix`): each synced desktop runs a
local `aw-server` under systemd, `awatcher` for window/AFK capture, and `aw-watcher-tmux`;
a user timer (default 5min) runs `aw-importer`, which only ever GETs from the local
server and folds its buckets into `<device>::<bucket>` on the central one, deduping on
`(device, bucket, starttime, endtime, canonical-data)` in batched inserts. The local
store stays the device's authoritative history, and central downtime only buffers events
until the next run. Enabled on rugged, wyrm2, iguana, and atlas; a new desktop is a
`ducktape.activitywatch.sync` block in `nix/home/hosts/<host>.nix`. Phones are not wired
in (tracked in `cluster/k8s/TODO.md`).

## Query Auth

Humans browse through the Authentik-protected `activitywatch.allegedly.works` route.
Agents use the bearer-gated read route below; it is deliberately separate from the human
session path.

Gotchas (bite every consumer): `POST /api/0/query` requires the **trailing slash**
(`/api/0/query/`; nginx 301s otherwise and a redirected POST degrades to GET); transient
TLS connection resets occur (~1/20 calls) — retry once; bucket `last_updated` is always
`null` on this server — derive recency from each bucket's newest event.

### Console agent read route (egress-substituted bearer)

The console-owned Claude runner pool (`haku-runtime-sandbox`, the sandbox behind Haku's
egress proxy) can't do an OAuth exchange, so the mechanism mirrors aiquota's: a static
bearer the sandbox never actually holds.

- The read route is gated on its own token and allows read methods only — GET plus POST
  to `/api/0/query/` — so even a leaked read token can't write. The token is minted and
  SOPS-encrypted at `cluster/k8s/activitywatch/activitywatch-read-token.sops.yaml`, and
  the emberstack reflector mirrors it into both `haku-console` and the legacy
  `haku-egress-proxy` namespace.
- The sandbox template sets only the inert placeholder
  (`AW_READ_TOKEN: activitywatch-read-token-placeholder`). The colocated Console egress
  proxy substitutes the real read token on `activitywatch-read.allegedly.works` read requests
  (`cluster/k8s/haku/console/config.yaml`); the runtime never receives it. The dedicated iron
  proxy keeps the same substitution for legacy Claude pods.
- The host is pinned in the Haku-Claude egress fence
  (`cluster/k8s/agents/haku-egress-proxy/cnp-haku-claude-egress.yaml`) and the
  `ACTIVITYWATCH` group of `cluster/validation/test_egress_allowlists.py`.

So the agent queries with the placeholder in its environment:

```bash
curl -H "Authorization: Bearer $AW_READ_TOKEN" \
  https://activitywatch-read.allegedly.works/api/0/buckets/
```

**Deviation for runtimes outside the fence** (the Claude Code web home, any `kubectl` that
reads `haku-sandbox`): the same read token is ESO-mirrored into `haku-sandbox` as
`activitywatch-read-token` (`cluster/k8s/haku/workspaces/app/activitywatch-read-token-eso.yaml`,
store `kubernetes-activitywatch-secret-store`), and Haku reads it and calls the read route
directly. Same route, same read-only bound, no approval step — the placeholder path above
only works for pods whose traffic actually traverses the fence.

## Storage Debt

The durable store is one SQLite file on Proxmox-pinned node-local storage — an accepted
Proxmox-dependent service under the OVH-only invariants (<../decisions.md>): device
importers buffer locally and re-push through central downtime, and the central DB is
rebuildable by re-importing from the devices' own aw-servers for whatever history they
still hold.

- The SQLite storage benchmark (#2959, `debug/sqlite_storage_bench/README.md`) ruled
  SeaweedFS CSI out for this store; moving off `local-path-proxmox` needs another
  validated target or a backup strategy. Until then, treat the central DB as node-local
  state that is rebuilt from the devices, not restored.
- 10Gi is a starting budget, not a retention policy: window/AFK data should fit for a
  long time, but multi-device growth is unbounded and unmeasured now that four devices
  feed it.
- Agent read access is a static long-lived bearer (SOPS-minted, iron-substituted). A
  rotator-issued short-lived token handoff would be more hygienic.
