# haku-ci: a warm Bazel server across PRs

Design for keeping a **warm Bazel server** — the JVM daemon with its Skyframe loading + analysis
graph resident in RAM — alive across CI jobs on the in-cluster `haku-ci` runner, so successive
PRs skip loading, analysis, and unchanged-action execution instead of paying a full cold build
every time. Founding constraint: no BuildBuddy/RBE or any external CI — `haku-state` source never
leaves the cluster (`cluster/k8s/haku-ci/README.md`).

## Status: Tier 1/1.5 landed; Tier 2 deferred

The runner runs every CI job in a fresh, ephemeral job container, so all Bazel state — the
server, the output base, the `--disk_cache`, the repo cache — is discarded per job. Tier 1
(persistent output-base/disk/repo cache on a PVC; ducktape #3467) and Tier 1.5 (`validate` folded
into the `bazel` job so one warm server serves both; haku-state #24 + ducktape #3474, ~40 s/run
saved) landed and took the `bazel` job from ~500–575 s cold to ~59–132 s. Even with caches warm,
each run still pays one serial ~45 s load+analyze phase (profile-confirmed; latency-bound, not
resource-bound) — exactly what a server kept warm across runs (Tier 2) would remove.

Current state is `cluster/k8s/haku-ci/`: the KEDA `ScaledJob` migration (one job per pod,
ducktape #3861) later traded Tier 1's cross-job PVC for a pod-local `emptyDir`, so cache warmth
now spans only the Bazel invocations within one CI job — that README § "What this costs: Bazel
cache locality" records the trade and names the fix if it proves too slow (a shared
`--disk_cache` on a real volume).

## Tier 2: decided — defer, don't drop

For a single-tenant, personal-infra CI at current PR volume, ~45 s/run does not justify standing
up and babysitting a persistent cross-PR Bazel server — Skyframe staleness, workspace lifecycle
outside the ephemeral job container, cache GC, and foothold-bounce cadence are all new, always-on
failure modes. Everything below is the plan of record, to build when a trigger fires:

- PR / code-push volume grows enough that ~45 s/run aggregates to real wall-clock pain (the
  serial runner makes head-of-line blocking compound it).
- The analysis phase itself grows — a much larger `MODULE.bazel` graph or target count pushes
  load+analyze well past ~45 s.
- The isolation calculus changes (e.g. `haku-ci` ever runs non-Haku-authored code), which would
  independently force a rethink.

## Tier 2 design

A long-lived **dind builder container** — a dind container, not a pod sidecar, because the job's
`docker` CLI is pointed at the dind daemon and can only `exec` into containers dind manages.
Created lazily (create-if-missing by the first job of the pod's life, so no Deployment change),
running `sleep infinity` so the Bazel server one job starts survives for the next. Its workspace
and `~/.cache` (output base, disk cache, repo cache) live on a `local-path` PVC — node-pinned,
acceptable for a regenerable cache (a stranded PV means delete, re-provision, eat one cold
build). Jobs dispatch with `docker exec … ci-build.sh <sha> <targets>`; the script fetches the
sha from the in-cluster Forgejo and `git clean -fdx`s the workspace between PRs, while the output
base outside the workspace stays warm. The builder image bakes bazelisk + a JDK with the
egress-proxy CA already in the JVM truststore, built and stored in-cluster. **Push credentials
never enter the builder**: credentialed steps (registry push, git push) stay in the ephemeral job
container.

## The isolation call

Tier 2 removes the fresh-container-per-job property: a long-lived builder whose workspace and
cache are shared across every PR's build. The exposure is strictly **cross-PR within the same
perimeter** — same namespace, no Haku RBAC, same egress fence, no cluster creds — and `haku-ci`
is single-tenant, so the threat is "prompt-injected Haku poisons a later Haku build". Acceptable
**with** the mitigations: clean checkout per sha, credentials stay in the job container, a
periodic builder bounce to cap foothold longevity, and a cache-size cap (`--disk_cache` has no
auto-eviction).

## Alternatives

- **Tier 3 — in-cluster `bazel-remote` gRPC cache**: LRU-evicting, survives node moves,
  complements Tier 2 (remote cache for execution, warm server for analysis) — but no warm RAM on
  its own.
- **`actions/cache`**: the tar/upload/download/untar tax rivals the build itself for a multi-GB
  Bazel cache; rejected.

## Open questions

- **Builder-image build path** (chicken-and-egg): a plain `docker build` bootstrap step, or
  ducktape's `oci_image` pipeline → in-cluster registry?
- **Cache GC policy** — `--disk_cache` has no auto-eviction; prune cadence vs. adopting
  `bazel-remote`.
- **Bounce cadence** for the builder (foothold longevity vs. warmth).
- **Server memory budget** — start at ~4 GiB inside the dind limit, tune from the profile.

## Validation (the non-obvious checks)

- act_runner must not reap the named builder container between jobs — it should only remove the
  job containers it created.
- "Starting local Bazel server" must be absent on warm runs, and the `--profile` trace should
  show ~0 analysis time on a no-op diff.
