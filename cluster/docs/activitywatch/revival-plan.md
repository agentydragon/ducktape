# ActivityWatch revival plan

Burn-down for turning ingestion back on after the 2026-08-17 retirement
([README](README.md)). Each step leaves this list when its PR lands; the plan is
done — and deleted — when the central store is being fed correctly again and the
[README](README.md) no longer says "retired".

## Target

One central `aw-server` is the sole writer of its own SQLite file. Devices export
an immutable single-file snapshot; a CronJob folds those snapshots into the
central server **over the REST API**, never by opening the file. Re-running the
importer inserts nothing. This is what the retirement note demanded before
reconsidering: repo-owned stable identity, read-only source snapshots, and an
idempotency test over repeated import.

The importer that meets it exists: `@ducktape_activitywatch//importer`. It reads
each snapshot `immutable=1` read-only, takes device identity from the inbox
directory name (so two `localhost` browsers never merge), dedups on
`(device, bucket, starttime, endtime, canonical-data)`, and writes through
`aw_client_rust::AwClient`. Its test starts a real `aw_server` and imports into it
over HTTP. The dedup read is windowed to each source batch, so a re-import costs
the overlap, not the whole bucket's history.

What is left is everything around it: a snapshot exporter to replace
`aw-sync push`, packaging, the cluster CronJob, CI, and one end-to-end pass before
trusting it with every device.

## Open decision: the desktop export format

The importer reads the **aw-server-rust** schema. Desktops today run the
**Python** `aw-server` (`nix/home/services/activitywatch.nix` → `pkgs.activitywatch`),
whose peewee SQLite schema the importer cannot read. The exporter has to bridge
this, and which way decides the exporter PR:

- **Switch desktops to aw-server-rust** (we already build it). The desktop's own
  store is then rust-schema, and export is an atomic snapshot of a consistent copy
  (`VACUUM INTO`), no translation. Cost: a desktop server migration.
- **Export through the REST API.** Read every bucket + event from the local
  aw-server over HTTP (schema-independent) and write a fresh rust-schema snapshot.
  Cost: a small writer tool; keeps the Python desktop server.
- **Teach the importer the peewee schema too.** Most coupling, least attractive.

Resolve this before writing the exporter. The rest of the plan is independent of
the choice.

## Steps

1. **Desktop snapshot exporter** (nix). Replace the 5-minute
   `aw-sync … sync-advanced --mode push` timer in
   `nix/home/services/activitywatch.nix` with one that writes an immutable,
   consistent single-file snapshot to `~/.activitywatch-sync/<hostname>/aw.db`
   (per-host subdir, so the shared Syncthing folder delivers the inbox layout
   `/<device>/aw.db` the importer globs). No heartbeat amplification: a snapshot of
   the distinct rows, not an appended staging DB. Satisfies canary requirement 1.

2. **Package `aw_importer_bin` into an image.** Either add the binary to the
   existing `@ducktape_activitywatch//:image` or a slimmer importer image. It needs
   a writable `XDG_CACHE_HOME`/`HOME` in the pod — `AwClient` takes a
   `SingleInstance` lock under the cache dir.

3. **Replace the importer CronJob.** In
   `cluster/k8s/x/activitywatch/importer-cronjob.yaml`, swap the `aw-sync … --mode
pull` command for
   `aw_importer_bin --host activitywatch-write.activitywatch.svc.cluster.local --port 5600 /sync-inbox/*/aw.db`,
   mount the inbox **read-only**, and drop the busybox conflict-guard initContainer
   — the glob skips `*.sync-conflict-*` and the importer fails closed on any
   ambiguous device dir. Then un-suspend the Flux `Kustomization` and keep the
   read-only Authentik query route as-is.

4. **CI coverage.** Wire `@ducktape_activitywatch//importer:aw_importer_test` into
   a CI invocation. The root `//...` sweep skips this `.bazelignore`d module, so
   without this the importer regresses ungated.

5. **One end-to-end canary.** Before enabling for every device: a single real pass
   — desktop export → Syncthing → CronJob import → query the central server —
   proving no source mutation, no provenance collision, and a zero-insert re-run,
   the same gates the [importer canary](importer-canary.md) named. Only then add
   devices back per the README's "Adding More Devices".

## Not blocking

Agent credential hygiene (rotator-issued short-lived tokens instead of reflected
Authentik client-credentials) and moving the central DB off `local-path-proxmox`
are independent of ingestion correctness; they stay in the [README](README.md)'s
debt list, not here.
