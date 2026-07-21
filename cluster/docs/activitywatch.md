# ActivityWatch

Personal activity tracking via [aw-server-rust](https://github.com/ActivityWatch/aw-server-rust).
The cluster is the query surface; individual devices keep local ActivityWatch capture and push
`aw-sync` databases through Syncthing.

No built-in ActivityWatch auth is enabled. Read/query access goes through the
read-only proxy and, for public access, Authentik.

## Current Design

- **Query server**: `aw-server-rust` in `cluster/k8s/activitywatch`, device id
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
- **Importer**: an `activitywatch-importer` CronJob runs every five minutes with
  `aw-sync sync --mode pull` directly against the Syncthing inbox. An init container
  fails closed unless every discovered database has the canonical
  `<device-id>/test.db` shape, so Syncthing conflict copies are never imported. The
  importer mounts the inbox read-write because upstream `aw-sync` opens remote SQLite
  databases through its writable datastore implementation and unconditionally creates
  `/sync-inbox/activitywatch-cluster/test.db`, even in pull-only mode. Receive-only
  Syncthing keeps those cluster-local changes from propagating to desktop peers.
- **Image**: `ghcr.io/agentydragon/aw-server`, built with Bazel
  (`@ducktape_activitywatch//:image`). The image includes both `aw-server` and upstream
  `aw-sync`. The pinned `aw-sync` source is patched to use reqwest's rustls backend
  instead of native-tls/OpenSSL, which avoids vendored OpenSSL build-script runfiles in
  Bazel/RBE.
- **Cluster Syncthing identity**: public certificate in
  `cluster/k8s/activitywatch/syncthing-identity.yaml`; private key only in
  `cluster/k8s/activitywatch/syncthing-key.sops.yaml`. The device ID in
  `syncthing-config.xml` is derived from that public certificate and checked in CI.
- **Cluster Syncthing config**: `cluster/k8s/activitywatch/syncthing-config.xml`
  declares the `activitywatch` receive-only folder and paired devices. An initContainer
  stages that ConfigMap into Syncthing's writable config dir before the Syncthing container
  starts; cert/key stay Kubernetes-owned and are mounted read-only at the filenames
  Syncthing expects.
- **No mesh sidecar**: ActivityWatch is not joined to Nebula. Devices send data through
  Syncthing, and query access should use an explicit in-cluster or authenticated route.

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
- If another `*.sync-conflict-*.db` appears after the Syncthing index is persistent,
  suspend the importer and investigate before deleting it. The importer intentionally
  refuses to run while any non-canonical database is present.
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

## Spike Results

On 2026-07-06, the local spike used two temporary ActivityWatch servers:

1. Source server `127.0.0.1:5666`, device id `rugged-spike`.
2. Receiver server `127.0.0.1:5667`, device id `cluster-spike`.
3. Inserted one `currentwindow` event into bucket `aw-watcher-window_rugged-spike`.
4. Ran `aw-sync --sync-dir /tmp/aw-spike-sync.YBIGhW/rugged sync-advanced --mode push --start-date 2026-07-06`.
5. This created `/tmp/aw-spike-sync.YBIGhW/rugged/rugged-spike/test.db` and synced one event.
6. Ran `aw-sync --sync-dir /tmp/aw-spike-sync.YBIGhW/rugged sync-advanced --mode pull --start-date 2026-07-06` against the receiver.
7. The receiver created bucket `aw-watcher-window_rugged-spike-synced-from-rugged` with
   bucket data `{"$aw.sync.origin":"rugged"}` and the original event.

That spike used an extra host folder because the first design kept one Syncthing folder per
source host. The current design flattens the Syncthing folder: each ActivityWatch server
writes only its own `<sync-root>/<aw-device-id>/test.db`, and imports preserve provenance
by appending `-synced-from-<bucket-hostname>` to cluster-side bucket IDs.

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

Each device contributes its own ActivityWatch device-ID subdirectory under the shared root,
so no cluster-side per-host folder or importer sidecar is needed for normal desktops.

Phones need a separate pass. ActivityWatch Android has sync-facing code upstream, but this
repo has not yet wired a phone into the Syncthing folder topology or verified Android
background behavior.

## Validation

Last checked 2026-07-21:

- `bazelisk build --config=rbe @ducktape_activitywatch//:image` passed; `aw-sync` and
  `aw_sync_bin` compiled and the OCI image assembled.
- `kustomize build cluster/k8s/activitywatch` passed.
- Syncthing 2.0.10 smoke test passed with the static ConfigMap-style
  `syncthing-config.xml` shape: flat `activitywatch` receive-only folder, desktop peers,
  and cluster self device.
- Focused Nix evals passed for `ducktape.activitywatch.sync`, the Syncthing `activitywatch`
  send-only folder, and the paired cluster device ID.
- The local nixpkgs `aw-sync` supports the desktop push timer command with
  `sync-advanced --mode push`.
- The pinned `@ducktape_activitywatch` source commit supports explicit pull-only mode,
  bounded 5,000-event chunks, and resuming from the destination's newest event.
- Full `nix build .#nixosConfigurations.rugged.config.system.build.toplevel --no-link`
  still fails before ActivityWatch on the unrelated
  `nix/packages/gaffer.nix` `builtins.fetchClosure` blocker.
