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
- **Packaging** (#4746): `aw-importer` ships as a CI-released artifact plus a guarded
  nix package; `release.yml` runs `aw_importer_test` before publishing, which is the
  module's only CI coverage (the PR `//...` sweep skips this `.bazelignore`d module).
- **Desktop scheduling (rugged canary)**: `nix/home/services/activitywatch.nix` runs
  the importer on a timer against the central write route, `AW_DEST_TOKEN` from the
  shared Secret, replacing the aw-sync push. rugged is importer-only.
- **Canary proven + second device**: rugged imports end-to-end — window/afk/web/tmux land
  under `rugged::…` on the central, deduped and idempotent (a re-run inserts only genuinely
  new events). wyrm2 is enabled as a second device (`wyrm2::…`) to exercise multi-device
  separation. The write-proxy needed a raised `client_max_body_size` (#4756): the importer
  POSTs a whole bucket per request and the ~10 MB backfill blew past nginx's 1 MB default.
- **All desktops enabled**: iguana and atlas join rugged and wyrm2 as importer devices
  (`iguana::…` / `atlas::…`). Config-only — the shared write token already reaches both
  hosts' user keys — so each starts feeding the central on its next `switch`.
- **Batched inserts**: the importer now POSTs a bucket's new events in fixed-size batches
  (`INSERT_BATCH_SIZE`), so a first backfill is many bounded requests, not one ~10 MB one —
  the write-proxy's 256 MB cap is now a safety ceiling, not a dependency, and could be
  lowered.

## What's left

1. **Incremental sync, then make the write route ingest-only.** v1 reads the whole source
   and whole destination bucket each run; switch to a per-bucket high-water mark — read only
   source events past the newest already in the destination. Once the importer no longer
   reads the destination, restrict the write route to write methods: today it must allow GET,
   so a leaked token can read history, not just ingest.

## Not blocking

Agent credential hygiene (rotator-issued short-lived tokens) and moving the central
DB off `local-path-proxmox` stay in the [README](README.md)'s debt list, not here.
