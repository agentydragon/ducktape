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

## Landed

- **Importer auth** (#4742): the importer reaches an HTTPS, bearer-gated central via
  `--dest-url` + `AW_DEST_TOKEN` (env only, never argv).
- **Cluster write path**: the central aw-server is revived behind a bearer-checking
  write-proxy sidecar, reached at `https://activitywatch-write.allegedly.works`. The
  shared token is a dual-recipient SOPS secret (cluster-secrets + the synced
  desktops' user keys), so one value serves both the proxy and each desktop's
  `AW_DEST_TOKEN`. aw-server itself and the Authentik read path are untouched, and
  the cluster Syncthing receiver and old aw-sync cronjob are removed rather than
  revived.

## What's left

1. **Package + schedule on the desktop** (nix). Replace the `aw-sync … --mode push`
   timer in `nix/home/services/activitywatch.nix` with the importer on a timer:
   `aw_importer_bin --dest-url https://activitywatch-write.allegedly.works --device <host>`,
   `AW_DEST_TOKEN` decrypted from the shared SOPS secret. The importer needs a
   writable `XDG_CACHE_HOME`/`HOME` — `AwClient` takes a `SingleInstance` lock there.

2. **Retire Syncthing (desktop side).** The cluster receiver, its certs, and the old
   aw-sync cronjob are gone; what remains is the desktop send-only folder and the
   per-host Syncthing certs/keys — a deletion across
   `nix/home/services/activitywatch.nix` and `secrets/home/<host>/` (plus the now
   unused `//secrets/home:activitywatch_syncthing_files` filegroup).

3. **CI coverage.** Gate `@ducktape_activitywatch//importer:aw_importer_test`: the
   root `//...` sweep and the PR bazel-diff both skip this `.bazelignore`d module, so
   it regresses ungated otherwise.

4. **Incremental sync, then make the write route ingest-only.** v1 reads the whole
   source and whole destination bucket each run; switch to a per-bucket high-water
   mark — read only source events past the newest already in the destination. Once
   the importer no longer reads the destination, restrict the write route to write
   methods: today it must allow GET, so a leaked token can read history, not just
   ingest.

5. **One end-to-end canary.** A single real pass — desktop importer → central server
   → query — before enabling every device, then add devices back per the README's
   "Adding More Devices".

## Not blocking

Agent credential hygiene (rotator-issued short-lived tokens) and moving the central
DB off `local-path-proxmox` stay in the [README](README.md)'s debt list, not here.
