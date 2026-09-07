# Where test container images live, and what each option costs

Every `requires_docker` test that needs Postgres loads `pgvector/pgvector:pg18` into a Docker
daemon before it can start a container. 48 targets do this (39 pgvector, 9 `postgres:18`), each
pays it in full, and none of them share the result. This doc records what the cost actually is,
which options exist for removing it, and the two that were measured and lost.

## The load is per action, not per machine

`load_oci_image` (<../../util/oci.py>) is built around one load per tag per machine: a marker
file in `/tmp` plus a per-tag `flock`. **On RBE that never fires.** Three sequential runs of the
same target returned three different `/proc/sys/kernel/random/boot_id` values, an empty `/tmp`,
and `docker images` empty with the daemon healthy. `recycle-runner: "true"` on
[`//:rbe_linux_x64`](../../BUILD.bazel) recycles the _workspace_, not the VM — each action gets a
freshly booted microVM with an empty image store.

`runner-recycling-max-wait` does not change this. With it set to `30s` and `--jobs=1`, so actions
could not overlap, four consecutive runs still produced four distinct boot ids and four empty
daemons.

The marker and the lock are therefore local-development mechanisms under current RBE behaviour.
That sits awkwardly with <../../debug/2026_08_14_docker_test_timeouts.md>, whose diagnosis assumed
concurrent targets sharing one daemon per worker; either the platform behaved differently in
August 2026 or that contention was only ever reachable locally. Worth resolving before anyone
relies on the lock for anything remote — it is harmless either way, but its comment claims more
than the current platform delivers.

## What it costs

| Quantity                                      | Measured                                     |
| --------------------------------------------- | -------------------------------------------- |
| pgvector layout                               | 158.9 MB compressed → 453.2 MB raw, 17 blobs |
| Cold load into the daemon                     | 11.8–21.1s (median ~14s)                     |
| Postgres accepting connections after the load | 1.98s                                        |
| Second load in the same process               | 0.01s                                        |
| Targets loading a postgres-family image       | 48                                           |
| Executor time per full sweep                  | **~11 min**                                  |

The load is the whole cost; starting Postgres afterwards is two seconds. `ryuk` (6.8 MB) adds
~1.2s to each of 46 targets.

## Option 1 — run Postgres as a process, not a container

Removes the load (~14s), the container start (~2s) and the ryuk load (~1.2s) for all 48 targets,
with no new image and no cache-key argument. It needs a postgres+pgvector build available to
Bazel and a rewrite of the fixtures in <../../util/testing/postgres_fixtures.py>.

This is the recommended direction. It is the only option whose saving does not have to be traded
against a boot cost, which matters because of what option 2 measured.

## Option 2 — bake the image into the action's VM rootfs

`docker info` reports `DockerRootDir=/var/lib/docker`, and `/proc/self/mountinfo` shows
`ROOT=/var/lib/docker POINT=/var/lib/docker` on the same device as `/` — a self-bind mount of the
rootfs, not a fresh directory mounted over it. BuildBuddy builds that rootfs from the action's
`container-image` exec property, so an image can ship a pre-populated daemon store.

**This works, end to end.** An image with pgvector baked into `/var/lib/docker`, run on two
freshly booted VMs, reported `IMAGES=pgvector/pgvector:pg18` and had Postgres accepting
connections in **3s**, against ~16s today. Baked content survives dockerd's initialisation and
sits alongside the `overlay2`/`image`/`containerd` directories it creates.

Two mechanics constrain the build:

- `init-dockerd=true` crashes the VM outright if `docker` is not on the image's `PATH`
  (`Firecracker VM crashed: exec: "docker": executable file not found`). dockerd comes from the
  container image, not the guest. `rbe-worker` already satisfies this.
- The bake must pull **by the digest pinned in `MODULE.bazel`**, not by the floating tag, or the
  Bazel pin and the baked image drift apart.
- `load_oci_image` would still reload, because `_already_loaded` returns `False` when the `/tmp`
  marker is absent. `/tmp` is on the rootfs too, so the image should ship the marker alongside the
  store; the layout digest and the daemon image id are both known at bake time.

### Why this is not the recommendation

A bigger rootfs costs measurably more to boot, and — more importantly — a niche image never gets
warm. 36 interleaved executions of two images differing only by the ~450 MB baked layer:

|                           | small base    | base + baked store |
| ------------------------- | ------------- | ------------------ |
| First-touch setup, median | 10.43s        | 15.13s             |
| p10 / p90                 | 8.10 / 18.30s | 12.16 / 19.52s     |
| Repeat-executor setup     | not observed  | **0.04s**          |

The extra layer costs **~4.7s per executor on first touch**, about 0.010 s/MB. That alone would be
an easy trade. The problem is the hit rate: those 36 runs landed on 34 distinct executors, and two
collisions in 36 draws puts the pool near 300. The one repeat hit cost 0.04s — free — but repeats
essentially do not happen.

`rbe-worker` is warm on effectively every executor _because every action uses it_, which is why
production setup time is ~0.04s. A second image used by only 48 targets per sweep would be cold
most of the time:

|                | today    | preloaded, warm | preloaded, cold |
| -------------- | -------- | --------------- | --------------- |
| Setup          | ~0.04s   | ~0.04s          | ~15–20s         |
| Image load     | ~14s     | 0s              | 0s              |
| Postgres ready | ~2s      | 3s              | 3s              |
| **Total**      | **~16s** | **~3s**         | **~18–23s**     |

The cold column is a wash with today or slightly worse. The win only arrives once the image has
propagated across the pool, and 48 touches per sweep against ~300 executors needs several sweeps
to saturate and must then survive eviction and executor churn.

Baking into `rbe-worker` itself would get the warm hit rate, since that image is warm everywhere.
It is also what the root `AGENTS.md` forbids: that digest is in every action's cache key and in
the key for BuildBuddy's warm Firecracker snapshot pool, so a bump orphans the action cache and
dumps every snapshot. Not to be attempted without deciding that rule should bend, and not without
a sweep-frequency number to weigh the one-time propagation cost against the per-sweep saving.

## Option 3 — a smaller image

Weak, and dominated by option 2 because it needs a custom image build for the same effort.
pgvector publishes no alpine variant — the only `pg18` tags are `pg18`, `pg18-bookworm` and
`pg18-trixie`, and the two measured are no smaller than what is pinned now (`pg18` 158.9 MB,
`pg18-trixie` 164.0 MB). The nearest smaller base is `postgres:18-alpine` at 120.0 MB (−25%),
which ships no pgvector and would need the extension built against musl.

## Rejected: storing the layers uncompressed

This looks like the obvious win and is not. Decompression is genuinely most of the work — gunzip
alone accounts for 6.2s of an 11.75s load, turning 158.9 MB into 453.2 MB. But loading layers that
were **already decompressed on disk took 15.09s**, slower than the 11.75s compressed path:
streaming and writing 453 MB costs more than transferring 159 MB and inflating it. The measurement
favoured the uncompressed arm, whose tars had just been written and were still in page cache, and
it lost anyway.

## Rejected: leaning on runner recycling

There is no VM reuse to lean on. See the boot-id result above.
