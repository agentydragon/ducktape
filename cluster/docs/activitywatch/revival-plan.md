# ActivityWatch revival plan

Burn-down for turning ingestion back on after the 2026-08-17 retirement
([README](README.md)). Each step leaves this list when its PR lands; the plan is
done — and deleted — when the central store is being fed correctly again and the
[README](README.md) no longer says "retired".

## Target

One central aw-server is the sole writer of its own SQLite file. Each device runs a
small importer that reads the device's own aw-server over the REST API and writes
the central one the same way — the correct shape of what upstream `aw-sync`
attempted: read-only at the source, provenance from the machine's identity,
idempotent on insert. Re-running inserts nothing.

The importer that meets it exists: `@ducktape_activitywatch//importer`. Given a
source and a destination aw-server and a device id, it folds every source bucket
into `<device>::<bucket>` on the destination, deduping on
`(device, bucket, starttime, endtime, canonical-data)`, and only ever GETs from the
source. Its test starts two real aw-servers and imports between them.

Reading the device's authoritative aw-server directly — instead of an `aw-sync`
staging copy — is also what removes the heartbeat amplification that retired the old
design: the server has already coalesced heartbeats into stored events, and the
importer dedups any residue.

## What's left

1. **Package + schedule on the desktop** (nix). Replace the `aw-sync … --mode push`
   timer in `nix/home/services/activitywatch.nix` with the importer on a timer:
   `aw_importer_bin --source-host 127.0.0.1 --dest-host <central> --device <host>`.
   The importer needs a writable `XDG_CACHE_HOME`/`HOME` — `AwClient` takes a
   `SingleInstance` lock there.

2. **Give the desktop a path to the cluster write API.** The one open decision
   (below). The central write service is admitted only from inside the cluster
   today; a desktop-run importer needs either an authenticated write route or a mesh
   route to reach it.

3. **Retire Syncthing.** With the importer talking to the server directly, the whole
   file-transport layer goes: the cluster Syncthing receiver, the desktop send-only
   folder, and the per-host Syncthing certs/keys — a deletion across
   `cluster/k8s/x/activitywatch/`, `nix/home/services/activitywatch.nix`, and
   `secrets/home/<host>/`.

4. **CI coverage.** Gate `@ducktape_activitywatch//importer:aw_importer_test`: the
   root `//...` sweep and the PR bazel-diff both skip this `.bazelignore`d module, so
   it regresses ungated otherwise.

5. **Incremental sync** (efficiency). v1 reads the whole source bucket and whole
   destination bucket each run; switch to a per-bucket high-water mark — read only
   source events past the newest already in the destination.

6. **One end-to-end canary.** A single real pass — desktop importer → central server
   → query — before enabling every device, then add devices back per the README's
   "Adding More Devices".

## Open decision: how the desktop reaches the cluster

The importer runs on the desktop and must reach the central aw-server's write API.
Two ways, and this decides step 2:

- **Authenticated write route** — expose a write-capable route (Authentik, mirroring
  the existing read route) and hand each desktop a credential. In-pattern with the
  existing per-host secrets, but it is a new write surface on the public edge.
- **Nebula mesh** — join the central aw-server to the mesh and let desktops reach an
  internal route. No public write surface; the cost is joining AW to Nebula, which
  it deliberately is not on today.

## Not blocking

Agent credential hygiene (rotator-issued short-lived tokens) and moving the central
DB off `local-path-proxmox` stay in the [README](README.md)'s debt list, not here.
