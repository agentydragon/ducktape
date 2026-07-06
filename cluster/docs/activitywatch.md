# ActivityWatch

Personal activity tracking via [aw-server-rust](https://github.com/ActivityWatch/aw-server-rust).
The cluster is the query surface; individual devices keep local ActivityWatch capture and push
`aw-sync` databases through Syncthing.

No built-in ActivityWatch auth is enabled. Read/query access is still constrained by Kubernetes
NetworkPolicy, the read-only proxy, and the Nebula mesh path.

## Current Design

- **Query server**: `aw-server-rust` in `cluster/k8s/activitywatch`, SQLite on
  `activitywatch-data` (`local-path-proxmox`, 1Gi).
- **Read-only API**: nginx sidecar on Service `activitywatch-readonly`, allowing GET plus
  POST `/api/0/query` for sandbox consumers.
- **Write API**: internal Service `activitywatch-write`, admitted only from the importer
  CronJob by CiliumNetworkPolicy.
- **Sync receiver**: `activitywatch-syncthing` Deployment on OVH, receiving into
  `activitywatch-sync-inbox` (`seaweedfs-ovh`, RWX, 1Gi).
- **Importer**: `activitywatch-sync-import` CronJob every 5 minutes. It stages
  `/sync-inbox/rugged` into an `emptyDir`, then runs upstream `aw-sync` from the
  ActivityWatch image to pull events into the query server.
- **Image**: `ghcr.io/agentydragon/aw-server`, built with Bazel
  (`@ducktape_activitywatch//:image`). The image includes both `aw-server` and upstream
  `aw-sync`. The pinned `aw-sync` source is patched to use reqwest's rustls backend
  instead of native-tls/OpenSSL, which avoids vendored OpenSSL build-script runfiles in
  Bazel/RBE.
- **Cluster Syncthing identity**: SOPS Secret
  `cluster/k8s/activitywatch/syncthing-identity.sops.yaml`.

## Rugged Setup

Rugged is the first synced desktop.

- Local capture is managed by the graphical ActivityWatch applet (`aw-qt`), which starts
  `aw-server`, `aw-watcher-afk`, and `aw-watcher-window`.
- `aw-qt` does not manage this Syncthing/`aw-sync` transport; Home Manager owns sync.
- `nix/home/services/activitywatch.nix` configures local clients to use
  `127.0.0.1:5600` when `ducktape.activitywatch.sync.enable = true`.
- A Home Manager timer runs every 5 minutes:
  `aw-sync --host 127.0.0.1 --port 5600 --sync-dir ~/.activitywatch-sync/rugged sync-advanced --mode push --start-date 2026-07-06`.
- Home Manager also enables Syncthing with a send-only folder:
  `~/.activitywatch-sync/rugged`, folder id `activitywatch-rugged`.
- Rugged's Syncthing keypair is SOPS-managed in
  `secrets/home/rugged/activitywatch-syncthing.yaml`.

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

The important operational result: each source device writes only its own
`<sync-root>/<hostname>/<device-id>/test.db`, and imports preserve provenance by appending
`-synced-from-<hostname>` to cluster-side bucket IDs.

## Adding More Devices

For another desktop such as `wyrm2`:

1. Generate a Syncthing cert/key pair for the device and store it under that host's
   `secrets/home/<host>/activitywatch-syncthing.yaml`.
2. Add the device ID to `activitywatch-syncthing-entrypoint.sh`.
3. Add a receive-only folder on the cluster, for example `activitywatch-wyrm2` at
   `/sync-inbox/wyrm2`.
4. Enable `ducktape.activitywatch.sync` in `nix/home/hosts/<host>.nix` with
   `hostname = "<host>"` and a send-only Syncthing folder
   `~/.activitywatch-sync/<host>`.
5. Add the host folder to the importer CronJob, or split importers per host if the list grows.

Phones need a separate pass. ActivityWatch Android has sync-facing code upstream, but this
repo has not yet wired a phone into the Syncthing folder topology or verified Android
background behavior.

## Validation

Last checked 2026-07-06:

- `bazelisk build --config=rbe @ducktape_activitywatch//:image` passed; `aw-sync` and
  `aw_sync_bin` compiled and the OCI image assembled.
- `kustomize build cluster/k8s/activitywatch` passed.
- Syncthing 2.0.10 CLI smoke test passed for the entrypoint's `generate`, `serve`,
  device `add-json`, and folder `add-json` sequence.
- Focused rugged Nix evals passed for `ducktape.activitywatch.sync`, the Syncthing
  `activitywatch-rugged` send-only folder, and the paired cluster device ID.
- Full `nix build .#nixosConfigurations.rugged.config.system.build.toplevel --no-link`
  still fails before ActivityWatch on the unrelated
  `nix/packages/gaffer.nix` `builtins.fetchClosure` blocker.
