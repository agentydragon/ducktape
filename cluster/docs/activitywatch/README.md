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
SQLite at `/data/db.sqlite3` on `data` (`piraeus-hdd-local` on wyrm2, 10Gi), plus
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

Gotchas (bite every consumer):

- The query body is `{"timeperiods": ["<start>/<end>"], "query": ["<awq>"]}` — **both values are
  arrays of strings**, the timeperiod one ISO 8601 interval. `timeperiod` singular, a float, an epoch
  pair, or `query` as a bare string each get a Rocket 422 that names neither field. Inside `query`,
  **`;` terminates each statement**; without it you get a `ParsingError`, which does name the token.
- `GET` on the query endpoint answers **404** in either spelling — aw-server implements it POST-only,
  despite the route table above reading "GET + POST". Query with `POST`. The trailing slash is
  optional (both spellings reach the same handler); prefer it, it is what the server documents.
- **No event cap** on `query_bucket` or on `events` REST (which honours an explicit `limit=`): one
  9-day `query_bucket` returned 36,848 events. The window is the whole cost — a 4-month one streams
  ~75 MB and dies in JSON parsing.
- `last_updated` is always `null` in `/api/0/buckets` — take recency from each bucket's newest event.
- A `503` at a flat **5.0 s** reading `reset reason: connection timeout` is the Gateway failing to
  connect to this pod, not this pod refusing you: a healthy read is ~0.1 s. Retry it. Rare and
  bursty on this route — one 503 in the first 40 reads today, none in the ~200 after it — so budget a
  couple of retries and trust no fixed rate.

### Read route (static bearer)

The agent can't do an OAuth exchange, so the read route is gated on a static bearer of its
own and allows read methods only — GET plus POST to `/api/0/query/` — so even a leaked read
token can't write. The token is minted and SOPS-encrypted at
`cluster/k8s/activitywatch/activitywatch-read-token.sops.yaml`. It is ESO-mirrored into
`haku-sandbox` as `activitywatch-read-token` (`cluster/cdk8s/haku/workspaces.py`, store
`kubernetes-activitywatch-secret-store`), and Haku reads it and calls the read route
directly:

```bash
curl -H "Authorization: Bearer $AW_READ_TOKEN" \
  https://activitywatch-read.allegedly.works/api/0/buckets/
```

agentplane-staging sandboxes hold no copy: their egress proxy substitutes the token for the
`agentplane-credential-activitywatch-read` placeholder on the read route's API
(`cluster/cdk8s/agentplane/egress_staging_credentials.py`).

## Storage Debt

The durable store is one SQLite file on Proxmox-pinned node-local storage — an accepted
Proxmox-dependent service under the OVH-only invariants (<../decisions.md>): device
importers buffer locally and re-push through central downtime, and the central DB is
rebuildable by re-importing from the devices' own aw-servers for whatever history they
still hold.

- The SQLite storage benchmark (#2959, `cluster/debug/sqlite_storage_bench/README.md`) ruled
  SeaweedFS CSI out for this store. `piraeus-hdd-local` keeps one copy on wyrm2, so treat
  the central DB as node-local state that is rebuilt from the devices, not restored.
- 10Gi is a starting budget, not a retention policy: window/AFK data should fit for a
  long time, but multi-device growth is unbounded and unmeasured now that four devices
  feed it.
- Agent read access is a static long-lived bearer (SOPS-minted, iron-substituted). A
  rotator-issued short-lived token handoff would be more hygienic.
