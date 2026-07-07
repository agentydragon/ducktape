# ActivityWatch

Personal activity tracking via [aw-server-rust](https://github.com/ActivityWatch/aw-server-rust).
The cluster is the query surface; individual devices keep local ActivityWatch capture and push
`aw-sync` databases through Syncthing.

No built-in ActivityWatch auth is enabled. Read/query access is constrained by Kubernetes
NetworkPolicy and the read-only proxy; there is no public or Nebula route.

## Current Design

- **Query server**: `aw-server-rust` in `cluster/k8s/activitywatch`, device id
  `activitywatch-cluster`, SQLite on `activitywatch-data` (`local-path-proxmox`, 10Gi).
- **Read-only API**: nginx sidecar on Service `activitywatch-readonly`, allowing GET plus
  POST `/api/0/query` for sandbox consumers.
- **Write API**: internal Service `activitywatch-write`, admitted only from the
  Syncthing/importer pod by CiliumNetworkPolicy.
- **Sync receiver**: `activitywatch-syncthing` Deployment on OVH, receiving into
  `activitywatch-sync-inbox` (`seaweedfs-ovh`, RWX, 10Gi). The cluster Syncthing
  folder is receive-only.
- **Importer**: an `aw-sync daemon` sidecar in the Syncthing Deployment. It points at
  `/sync-inbox`, pulls synced device DBs into the query server, and writes the cluster's
  own non-authoritative `/sync-inbox/activitywatch-cluster/test.db`. This uses the
  upstream daemon loop instead of a repo-owned shell loop.
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
  declares the `activitywatch` receive-only folder and paired devices. The entrypoint only
  stages that config plus the identity files and execs Syncthing.
- **No mesh sidecar**: ActivityWatch is not joined to Nebula. Devices send data through
  Syncthing, and query access should use an explicit in-cluster or authenticated route.

## Storage Debt

The Syncthing inbox is intentionally on SeaweedFS (`activitywatch-sync-inbox`,
`seaweedfs-ovh`). The query server is still the risky piece: ActivityWatch's durable store
is one SQLite file on `activitywatch-data` (`local-path-proxmox`), and the server is pinned
to Proxmox to stay near that local-path PVC.

Both PVCs request 10Gi. That is deliberately a starting budget, not a retention policy:
window/AFK data should usually fit for a long time at that size, but multi-device history is
unbounded until we measure real growth on rugged and later devices.

TODO:

- Resolve the SQLite benchmark issue (#2959) before putting the hot query DB on SeaweedFS
  CSI or any other replicated POSIX-ish layer.
- If Syncthing's receive-only status is noisy because `aw-sync daemon` writes
  `/sync-inbox/activitywatch-cluster/test.db`, patch or wrap `aw-sync` with a pull-only
  daemon mode. Do not reintroduce per-host staging folders just to avoid that local file.
- Move the query server off `local-path-proxmox` once a validated storage target or backup
  strategy exists. Until then, treat the cluster DB as node-local state that must be backed
  up or rebuildable from synced device folders.
- Do not add additional ActivityWatch devices in a way that makes the local-path query DB
  the only durable copy of their data; each device should keep its own Syncthing-exported
  source folder.

## Desktop Setup

Current synced desktops are `rugged`, `wyrm2`, `iguana`, and `atlas`.

- Local capture is managed by the graphical ActivityWatch applet (`aw-qt`), which starts
  `aw-server`, `aw-watcher-afk`, and `aw-watcher-window`.
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

Last checked 2026-07-07:

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
- The pinned `@ducktape_activitywatch` source commit supports the cluster sidecar command
  with `daemon`.
- Full `nix build .#nixosConfigurations.rugged.config.system.build.toplevel --no-link`
  still fails before ActivityWatch on the unrelated
  `nix/packages/gaffer.nix` `builtins.fetchClosure` blocker.
