# ActivityWatch

**Revived 2026-08-26.** The repo-owned idempotent importer this retirement waited for
(`@ducktape_activitywatch//importer`) now feeds the central store: each device reads its
own aw-server over REST and pushes into the central one over a bearer-gated write route at
`https://activitywatch-write.allegedly.works` — no snapshot files, no `aw-sync`, no
Syncthing. The central store and write route are live; rugged and wyrm2 feed it, deduped
and idempotent (re-running inserts only genuinely new events). Remaining rollout — more
devices, retiring the desktop Syncthing material, and switching to incremental/chunked
writes — is tracked in [revival-plan.md](revival-plan.md).

**History.** Retired 2026-08-17 because the receive-only input could not be imported
correctly with upstream `aw-sync`: it mutated SQLite inputs, lost provenance, and was not
idempotent. The design below is that historical `aw-sync`/Syncthing wiring, retired and
replaced; it does not describe the running service.

Personal activity tracking via [aw-server-rust](https://github.com/ActivityWatch/aw-server-rust).
The cluster is intended to be the query surface; individual devices keep local ActivityWatch
capture and currently push `aw-sync` staging databases through Syncthing.

This directory is the documentation hub for the cluster ActivityWatch project:

- [Importer canary](importer-canary.md): why upstream `aw-sync` ingestion is suspended.
- [Syncthing conflicts](syncthing-conflicts.md): receiver index-loss and conflict-file RCA.
- [GNOME Wayland capture](gnome-wayland-capture.md): missing window events on `rugged`.

The first-generation `aw-sync` pull canary — which exposed the source mutation, provenance
collisions, and heartbeat amplification that forced the 2026-08-17 retirement — is recorded
in [the importer canary record](importer-canary.md). The current instance-to-instance
importer avoids all three (read-only at the source, provenance from the device id, idempotent
insert); see the status at the top of this file.

No built-in ActivityWatch auth is enabled. Read/query access goes through the
read-only proxy and, for public access, Authentik. The write path is bearer-gated.

## Former Design

- **Query server**: `aw-server-rust` in `cluster/k8s/x/activitywatch`, device id
  `activitywatch-cluster`, SQLite on `activitywatch-data` (`local-path-proxmox`, 10Gi).
- **Read-only API**: nginx sidecar on Service `activitywatch-readonly`, allowing GET plus
  POST `/api/0/query` for query consumers.
- **Public query route**: `https://activitywatch.allegedly.works` routes through the
  shared Authentik embedded proxy outpost to `activitywatch-readonly`. Human access is
  gated by Authentik group `activitywatch-users` (currently `agentydragon`).
- **Write API**: internal Service `activitywatch-write`, admitted only from the
  Syncthing/importer pod by CiliumNetworkPolicy.
- **Sync receiver**: `activitywatch-syncthing` Deployment on OVH, pinned with
  `topology.kubernetes.io/zone: hil-ovh`, and receiving into
  `activitywatch-sync-inbox` (`seaweedfs-ovh`, RWX, 10Gi). The cluster Syncthing
  folder is receive-only. Syncthing's index lives separately on
  `activitywatch-syncthing-state` (`local-path-ovh`, RWO, 1Gi); losing that index while
  retaining the inbox makes Syncthing treat the existing files as untracked local changes
  and create conflict copies when peers reconnect. OVH nodes have `region: hil` and
  `zone: hil-ovh`; do not use `region: hil-ovh`, which matches no node.
- **Importer**: an `activitywatch-importer` CronJob is configured for five-minute
  `aw-sync sync --mode pull` runs directly against the Syncthing inbox, but
  `spec.suspend: true` prevents scheduled execution. An init container
  fails closed unless every discovered database has the canonical
  `<device-id>/test.db` shape, so Syncthing conflict copies are never imported. The
  importer mounts the inbox read-write because upstream `aw-sync` opens remote SQLite
  databases through its writable datastore implementation and unconditionally creates
  `/sync-inbox/activitywatch-cluster/test.db`, even in pull-only mode. The canary proved
  that this also migrates source databases in place. Receive-only Syncthing does not make
  those writes safe: it classifies them as local changes and can create conflict copies
  when peer versions arrive.
- **Image**: `ghcr.io/agentydragon/aw-server`, built with Bazel
  (`@ducktape_activitywatch//:image`). The image includes both `aw-server` and upstream
  `aw-sync`. The pinned `aw-sync` source is patched to use reqwest's rustls backend
  instead of native-tls/OpenSSL, which avoids vendored OpenSSL build-script runfiles in
  Bazel/RBE.
- **Cluster Syncthing identity**: public certificate in
  `cluster/k8s/x/activitywatch/syncthing-identity.yaml`; private key only in
  `cluster/k8s/x/activitywatch/syncthing-key.sops.yaml`. The device ID in
  `syncthing-config.xml` is derived from that public certificate and checked in CI.
- **Cluster Syncthing config**: `cluster/k8s/x/activitywatch/syncthing-config.xml`
  declares the `activitywatch` receive-only folder and paired devices. An initContainer
  stages that ConfigMap into Syncthing's writable config dir before the Syncthing container
  starts; cert/key stay Kubernetes-owned and are mounted read-only at the filenames
  Syncthing expects.
- **No mesh sidecar**: ActivityWatch is not joined to Nebula. Devices send data through
  Syncthing, and query access should use an explicit in-cluster or authenticated route.

## Current Incident State

Verified 2026-07-21 UTC:

- Flux Kustomization `activitywatch` and CronJob `activitywatch-importer` are suspended.
- Deployments `activitywatch` and `activitywatch-syncthing` are each ready with one replica.
- Manual Job `activitywatch-importer-canary-1` completed, but its result failed the
  correctness gates below.
- The central query database was empty before the canary and now contains partial canary
  imports. It must be rebuilt before another test.
- The four source files were restored byte-for-byte from pre-canary snapshots before
  Syncthing restarted. Syncthing then correctly preserved the two restored local versions
  as new rugged/wyrm2 conflict files when newer peer versions arrived. Their hashes equal
  the recorded pre-canary snapshots, which attributes these conflicts to the manual
  restoration rather than another lost Syncthing index.
- Do not delete the new conflict files until their hashes and provenance in the canary
  record have been preserved. The importer rejects them and remains suspended.

## Query Auth

Humans browse to `https://activitywatch.allegedly.works` and authenticate through
Authentik. The Authentik proxy provider is named `activitywatch`; it forwards only to the
read-only nginx sidecar, not to the write-capable ActivityWatch service.

Agents use separate Authentik service accounts and reflected client credentials:

| Agent          | Authentik user                 | Reflected Secret                                  | Namespace        |
| -------------- | ------------------------------ | ------------------------------------------------- | ---------------- |
| Haku           | `activitywatch-haku`           | `activitywatch-haku-client-credentials`           | `haku-sandbox`   |
| Claude sandbox | `activitywatch-claude-sandbox` | `activitywatch-claude-sandbox-client-credentials` | `claude-sandbox` |

Each Secret contains:

- `client_id`, `username`, `password`, `source_scopes`: mint a source JWT at `token_url`.
- `proxy_client_id`, `proxy_scopes`: exchange that source JWT for an Authentik proxy
  bearer token scoped to the ActivityWatch proxy provider.
- `activitywatch_url`: the endpoint to query with `Authorization: Bearer <proxy-token>`.

The read-only service is NetworkPolicy-admitted from the Authentik server pods only; agent
pods should not reach it directly.

Rollout note: immediately after Terraform creates or updates the `activitywatch` proxy
provider, the HTTPRoute can exist before the embedded Authentik outpost has reloaded the
provider. In that transient window, `https://activitywatch.allegedly.works/api/0/info`
may return an Authentik 404. Wait for server logs to show
`authentik.outpost.proxyv2 Loaded application host=activitywatch.allegedly.works`; after
that, unauthenticated `/api/0/info` should return a proxy-auth redirect to
`/outpost.goauthentik.io/start`, not an Authentik 404.

The concrete mint (identical for every agent; verified 2026-07-07 with the haku secret):

```bash
# 1. Source JWT (1h): client-credentials with the service-account user.
curl -s "$token_url" -d grant_type=client_credentials -d client_id="$client_id" \
  -d username="$username" -d password="$password" -d scope="$source_scopes"
# 2. Proxy bearer (1h): jwt-bearer exchange against the proxy provider's client id.
curl -s "$token_url" -d grant_type=client_credentials -d client_id="$proxy_client_id" \
  -d scope="$proxy_scopes" \
  -d client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer \
  -d client_assertion="$source_access_token"
# 3. Query with the proxy bearer.
curl -s -H "Authorization: Bearer $proxy_access_token" "$activitywatch_url/api/0/buckets/"
```

Gotchas (bite every consumer): `POST /api/0/query` requires the **trailing slash**
(`/api/0/query/`; nginx 301s otherwise and a redirected POST degrades to GET); transient
TLS connection resets occur (~1/20 calls) — retry once; bucket `last_updated` is always
`null` on this server — derive recency from each bucket's newest event.

### Console agent read route (iron-substituted bearer)

The console-owned Claude runner pool (`haku-runtime-sandbox`, the sandbox behind Haku's
egress proxy) reads the central store over a **separate** bearer-gated route,
`https://activitywatch-read.allegedly.works` — not the Authentik path above. It can't do
the OAuth exchange, so the mechanism mirrors aiquota's: a static bearer the sandbox never
actually holds.

- The read route is a distinct `server` block on the `bearer-proxy` sidecar (port 5603),
  gated on its own read token and allowing read methods only — GET plus POST to
  `/api/0/query/` — so even a leaked read token can't write. The token is minted and
  SOPS-encrypted at `cluster/k8s/x/activitywatch/activitywatch-read-token.sops.yaml`, and
  the emberstack reflector mirrors it into `haku-egress-proxy`.
- The sandbox template sets only the inert placeholder
  (`AW_READ_TOKEN: activitywatch-read-token-placeholder`). The iron egress proxy
  substitutes the real read token on `activitywatch-read.allegedly.works` read requests
  (`cluster/k8s/agents/haku-egress-proxy/claude-iron.yaml`); the runtime never receives it.
- The host is pinned in the Haku-Claude egress fence
  (`cluster/k8s/agents/haku-egress-proxy/cnp-haku-claude-egress.yaml`) and the
  `ACTIVITYWATCH` group of `cluster/validation/test_egress_allowlists.py`.

So the agent queries with the placeholder in its environment:

```bash
curl -H "Authorization: Bearer $AW_READ_TOKEN" \
  https://activitywatch-read.allegedly.works/api/0/buckets/
```

## Storage Debt

The Syncthing inbox is intentionally on SeaweedFS (`activitywatch-sync-inbox`,
`seaweedfs-ovh`). The query server is still the risky piece: ActivityWatch's durable store
is one SQLite file on `activitywatch-data` (`local-path-proxmox`), and the server is pinned
to Proxmox to stay near that local-path PVC.

The two data PVCs request 10Gi; the rebuildable Syncthing index requests 1Gi. These are
deliberately starting budgets, not a retention policy:
window/AFK data should usually fit for a long time at that size, but multi-device history is
unbounded until we measure real growth on rugged and later devices.

TODO:

- Resolve the SQLite benchmark issue (#2959) before putting the hot query DB on SeaweedFS
  CSI or any other replicated POSIX-ish layer.
- Revisit whether the Syncthing config initContainer can avoid copying `config.xml` once
  we have a supported way to keep Syncthing's config file writable without staging it out
  of the ConfigMap.
- If a `*.sync-conflict-*.db` appears, keep the importer suspended and establish whether
  the file represents a receiver-local write, an index reset, or an independent peer
  history before deleting it. Receive-only prevents propagation; it does not prevent
  local writes or conflicts.
- Replace or repair the desktop export path before enabling ingestion. Repeated
  `aw-sync --mode push` runs amplified AFK and browser heartbeat rows in the exported
  staging databases; Atlas had 84,814 AFK rows but only 4,738 distinct
  `(starttime,endtime,data)` rows at canary time. This is a concrete amplified input
  behind the earlier database growth and memory pressure.
- Do not use direct upstream `aw-sync --mode pull` on the receive-only inbox. It opens
  and migrates sources read-write, creates its own `activitywatch-cluster/test.db`, and
  derives provenance from bucket hostname rather than the source device directory.
- Move the query server off `local-path-proxmox` once a validated storage target or backup
  strategy exists. Until then, treat the cluster DB as node-local state that must be backed
  up or rebuildable from synced device folders.
- Do not add additional ActivityWatch devices in a way that makes the local-path query DB
  the only durable copy of their data; each device should keep its own Syncthing-exported
  source folder.
- Revisit agent credentials: current agent access reflects persistent Authentik
  client-credential material into agent namespaces. A rotator-issued proxy JWT, or another
  auto-rotated short-lived token handoff, would be more hygienic if agents do not need to
  perform the OAuth exchange themselves.

## Desktop Setup

Current synced desktops are `rugged`, `wyrm2`, `iguana`, and `atlas`.

- Local capture is managed by the graphical ActivityWatch applet (`aw-qt`), which starts
  `aw-server`, `aw-watcher-afk`, and `aw-watcher-window`.
- Synced desktops get an XDG autostart entry for `aw-qt` from Home Manager.
- `aw-qt` does not manage this Syncthing/`aw-sync` transport; Home Manager owns sync.
- `nix/home/services/activitywatch.nix` configures local clients to use
  `127.0.0.1:5600` when `ducktape.activitywatch.sync.enable = true`.
- A Home Manager timer runs every 5 minutes:
  `aw-sync --host 127.0.0.1 --port 5600 --sync-dir ~/.activitywatch-sync sync-advanced --mode push`.
- Home Manager also enables Syncthing with a send-only folder:
  `~/.activitywatch-sync`, folder id `activitywatch`.
- Each desktop's Syncthing certificate is plaintext in
  `secrets/home/<host>/activitywatch-syncthing.cert.pem`; its private key is a raw SOPS
  binary secret in `secrets/home/<host>/activitywatch-syncthing.sops.key`.
- `aw-sync` runs without `--start-date`, so initial rollout backfills existing local
  ActivityWatch history instead of silently dropping pre-rollout events.

This desktop export path is the identified amplification source and must be replaced or
made idempotent. The files under `~/.activitywatch-sync` are staging databases, not the
desktop server's authoritative SQLite database.

## Superseded Spike Result

On 2026-07-06, the local spike used two temporary ActivityWatch servers:

1. Source server `127.0.0.1:5666`, device id `rugged-spike`.
2. Receiver server `127.0.0.1:5667`, device id `cluster-spike`.
3. Inserted one `currentwindow` event into bucket `aw-watcher-window_rugged-spike`.
4. Ran `aw-sync --sync-dir /tmp/aw-spike-sync.YBIGhW/rugged sync-advanced --mode push --start-date 2026-07-06`.
5. This created `/tmp/aw-spike-sync.YBIGhW/rugged/rugged-spike/test.db` and synced one event.
6. Ran `aw-sync --sync-dir /tmp/aw-spike-sync.YBIGhW/rugged sync-advanced --mode pull --start-date 2026-07-06` against the receiver.
7. The receiver created bucket `aw-watcher-window_rugged-spike-synced-from-rugged` with
   bucket data `{"$aw.sync.origin":"rugged"}` and the original event.

That spike proved only that a single push and pull could move one event. It did not exercise
repeated desktop pushes, a read-only Syncthing receiver, old database migrations, or two
browser buckets whose hostname is `localhost`. The live canary disproved the resulting
design assumptions:

- repeated pushes can amplify heartbeat-shaped rows in staging databases;
- pull mode writes to every opened SQLite database and creates a cluster-local staging DB;
- provenance from `bucket.hostname` collapses rugged and wyrm2 browser history into the
  same `...-synced-from-localhost` destination.

## Adding More Devices

For another desktop:

1. Generate a Syncthing cert/key pair for the device. Store the public certificate as
   `secrets/home/<host>/activitywatch-syncthing.cert.pem` and the private key as raw SOPS
   binary at `secrets/home/<host>/activitywatch-syncthing.sops.key`.
2. Add a SOPS binary rule for the private key in `.sops.yaml`.
3. Enable `ducktape.activitywatch.sync` in `nix/home/hosts/<host>.nix`, setting only
   `syncthing.certFile` and `syncthing.keySopsFile`. The shared Home Manager module owns
   the send-only Syncthing folder and the paired cluster device.
4. Add the cert-derived Syncthing device ID to `syncthing-config.xml`. The
   `//cluster/validation:test_activitywatch_syncthing_config` parity test fails if the XML
   device IDs drift from the public certificates or if a cert is missing its SOPS key.

Do not add another device until the export/import format is replaced or repaired. The next
design must have a repo-managed stable source identity, immutable or read-only source
snapshots, and an idempotency test using repeated exports and imports.

Phones need a separate pass. ActivityWatch Android has sync-facing code upstream, but this
repo has not yet wired a phone into the Syncthing folder topology or verified Android
background behavior.

## Validation

Last checked 2026-07-21:

- `bazelisk build --config=rbe @ducktape_activitywatch//:image` passed; `aw-sync` and
  `aw_sync_bin` compiled and the OCI image assembled.
- `kustomize build cluster/k8s/x/activitywatch` passed.
- Syncthing 2.0.10 smoke test passed with the static ConfigMap-style
  `syncthing-config.xml` shape: flat `activitywatch` receive-only folder, desktop peers,
  and cluster self device.
- Focused Nix evals passed for `ducktape.activitywatch.sync`, the Syncthing `activitywatch`
  send-only folder, and the paired cluster device ID.
- The local nixpkgs `aw-sync` supports the desktop push timer command with
  `sync-advanced --mode push`.
- The pinned `@ducktape_activitywatch` source commit supports explicit pull-only mode,
  bounded 5,000-event chunks, and resuming from the destination's newest event.
- A live pull-only canary failed: it migrated all four sources, created
  `activitywatch-cluster/test.db` in the inbox, collided both `localhost` browser
  buckets, and imported already-amplified Atlas AFK history. Passing build tests do not
  establish safe runtime semantics for this topology.
- Full `nix build .#nixosConfigurations.rugged.config.system.build.toplevel --no-link`
  still fails before ActivityWatch on the unrelated
  `nix/packages/gaffer.nix` `builtins.fetchClosure` blocker.
