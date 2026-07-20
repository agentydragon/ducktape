# haku-ci: a warm Bazel server across PRs

Design for keeping a **warm Bazel server** — the JVM daemon with its Skyframe loading +
analysis graph resident in RAM — alive across CI jobs on the in-cluster `haku-ci` runner, so
successive PRs skip loading, analysis, and unchanged-action execution instead of paying a
full cold build every time.

**Status:** proposal. It asks for exactly one operator decision — the shared-workspace
isolation trade-off (see [The isolation decision](#the-isolation-decision)). Nothing here has
landed. Manifests live in `cluster/k8s/haku-ci/` (this repo); workflow edits live in
`haku-state` (`.forgejo/`).

## Why

The runner runs every job in a **fresh, ephemeral job container** (`catthehacker/ubuntu`,
spawned by the `dind` sidecar and destroyed when the job ends). Bazel runs _inside_ that
container, so the Bazel server, the output base (`~/.cache/bazel`), the `--disk_cache`, and
the bzlmod repository cache — including a fresh clone of the in-cluster ducktape mirror on
_every_ invocation — are all thrown away per job. `cluster/k8s/haku-ci/config.yaml` also sets
`cache: enabled: false`, so the `actions/cache@v4` step in the runner-setup composite has no
backend and silently persists nothing.

The result is a flat cold-build cost on every run (measured from consecutive `main` runs,
Forgejo `/api/v1/repos/haku/haku-state/actions/tasks`):

| Job         | Wall time  | Work                                                       |
| ----------- | ---------- | ---------------------------------------------------------- |
| `bazel`     | ~500-575 s | `bazel test //...` + `bazel build //...` (image build)     |
| `validate`  | ~215-240 s | only `bazel run //tools:validate_state` + `freshness_lint` |
| `linkcheck` | ~55-85 s   | dind `docker build` + lychee                               |
| `lint`      | ~56-61 s   | dind `docker build` + ruff                                 |

`validate` spending ~220 s every run to execute a tiny Python validator, and `bazel` never
once dropping to an incremental number, are the tell: nothing is reused between runs.
`capacity: 1` serializes, and a PR into `main` fires `bazel` + `validate` + `linkcheck` (push
and PR each), so ~3 cold jobs queue back-to-back — ~13 min wall for one PR.

## Goal / non-goals

**Goal:** a Bazel server reused across PRs, so a no-op-diff `bazel test //...` returns in
seconds (Skyframe warm, only genuinely-changed actions execute). Target: `bazel` from
~500–575 s to ~10–60 s depending on the diff; `validate` from ~220 s to a few seconds.

**Non-goals:** introducing BuildBuddy/RBE or any external CI — the founding constraint is that
`haku-state` source never leaves the cluster (`cluster/k8s/haku-ci/README.md`,
`haku/console/docs/containment.md`). Widening the runner's trust perimeter (own namespace, no
Haku RBAC, egress-fenced, no cluster creds, off the control planes) is also out of scope; this
design stays inside it.

## Background: three lifetimes

```text
ephemeral job container   ⊂   long-lived runner pod (runner + dind)   ⊂   node
  dies after each job          up 9 days; capacity:1; Recreate            8 CPU / 32 GiB
  ← Bazel runs HERE today       ← state must move to HERE (or a PVC)
```

The runner _pod_ is already long-lived — "recycling the runner" is not the missing piece. The
state is discarded one layer down, in the per-job container. Tier 2 moves Bazel out of that
layer into a process that outlives it.

## Design

### Shape

A **long-lived Bazel-builder container**, created idempotently by the CI job, holding its
workspace + `~/.cache` (output base, disk cache, repo cache) on a persistent volume, running
`sleep infinity` so the Bazel server it spawns survives between jobs. CI jobs dispatch builds
into it with `docker exec`.

**Deviation from the usual "sidecar" instinct:** the builder is a **dind container**, _not_ a
pod container. The job's `docker` CLI is pointed at the dind daemon
(`DOCKER_HOST=tcp://host.docker.internal:2375`, set in `config.yaml`), so it can only `exec`
into containers dind manages — a pod-sibling container is invisible to it, a dind container is
not. Creating it lazily (create-if-missing) also means **no Deployment change to run it**: the
first job of the pod's life creates it, later jobs reuse it.

### Warm-server mechanics

The Bazel server is a JVM daemon started by the first `bazel` command and kept alive
(`--max_idle_secs`, default 3 h) as long as its container lives. Because the builder container
is `sleep infinity`, the server started by one job's `bazel test` keeps running, and the next
job's `docker exec … bazel test` is a thin client that connects to that **already-warm
server** — loading and analysis are incremental (Skyframe in RAM), and only changed actions
execute.

### What survives what

| State                                              | Across jobs | Across pod restart     |
| -------------------------------------------------- | ----------- | ---------------------- |
| Server RAM (Skyframe loading + analysis)           | ✅ warm     | ❌ inherent — new dind |
| Output base + `--disk_cache` + repo cache (on PVC) | ✅          | ✅ (on a PVC)          |

Server-RAM warmth is inherently pod-lifetime (a new pod is a new dind is a new builder is a
cold server). Backing `~/.cache` with a **PVC** means even a cold server after a pod restart
skips re-execution and re-download and only re-analyzes. A docker _named volume_ is the
simpler alternative but is pod-lifetime only (`docker-data` is `emptyDir`); prefer the PVC.

### Dispatch flow (the CI job step)

```bash
# Ensure the warm builder exists (idempotent; the pod's first job creates it).
docker inspect haku-bazel-builder >/dev/null 2>&1 || docker run -d \
  --name haku-bazel-builder \
  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY \
  -v haku-bazel-cache:/root/.cache \
  git.allegedly.works/haku/bazel-builder:latest sleep infinity

# Dispatch the checked-out commit into the warm server.
docker exec -e GITHUB_SHA="$GITHUB_SHA" haku-bazel-builder \
  /usr/local/bin/ci-build.sh "$GITHUB_SHA" "//..."
```

The job container already has the docker CLI + `DOCKER_HOST` + the proxy env, so it can both
create and drive the builder with no new privileges.

### `ci-build.sh` (inside the builder)

```bash
#!/usr/bin/env bash
set -euo pipefail
sha="$1"; shift
targets="${*:-//...}"
ws=/workspace/haku-state
[ -d "$ws/.git" ] || git clone http://haku:${MIRROR_TOKEN}@forgejo-http.forgejo:3000/haku/haku-state.git "$ws"
cd "$ws"
git fetch --quiet origin "$sha"
git checkout --quiet --force "$sha"
git clean -fdx                     # workspace only; ~/.cache (output base) is untouched
bazel test  --config=ci $targets
bazel build --config=ci $targets
```

`git clean -fdx` scrubs the workspace between PRs; the output base lives under `~/.cache`,
outside the workspace, so it is _not_ cleaned — that is what stays warm.

### Builder image

A small custom image baking bazelisk + a JDK, the egress-proxy CA already imported into the
JVM truststore, and a prepared `~/.bazelrc` (`--host_jvm_args` truststore + `--disk_cache`).
This also retires the per-run `tools/ci/trust_egress_ca.sh` keytool dance (a standing TODO in
both `.bazelrc` and `config.yaml`). The image is built and stored in the in-cluster Forgejo
registry — source and toolchain stay in-cluster.

### Source fetch / git auth

The builder fetches `haku-state` from the in-cluster Forgejo
(`forgejo-http.forgejo:3000/haku/haku-state.git`) with a repo-read token, mirroring the
existing `DUCKTAPE_MIRROR_READ_TOKEN` pattern in `bazel-runner-setup/action.yml`. All
in-cluster, all behind the egress fence.

### What changes, by file

- **`cluster/k8s/haku-ci/deployment.yaml`** (this repo): add a PVC (`local-path-ovh-hdd`,
  ~30–50 GiB) mounted into the `dind` container; back `haku-bazel-cache` with it. **Deviation**
  from the repo's `seaweedfs-ovh` default (`cluster/AGENTS.md` § Storage Selection): the cache
  is regenerable and latency-sensitive, so node-local wins — exactly the case the default
  carves out. **Tier gotcha:** the runner is fenced onto the KS-5 workers, labeled
  `storage.allegedly.works/tier=hdd`; the NVMe `-ssd` tier lives on the KS-GAME **control-plane**
  nodes the runner is pinned off of, so an `-ssd` PVC would never bind here. The cache is
  therefore HDD-backed; if it proves I/O-bound, re-tiering the worker or attaching a dedicated
  disk is the follow-up. Bump the `dind` limits — the warm JVM server + build actions run
  inside dind's cgroup; start at cpu `"6"` / mem `12Gi` (node has 8 CPU / 32 GiB headroom).
- **`cluster/k8s/haku-ci/config.yaml`** (this repo): no new job-container `-v` mounts needed —
  the builder holds the cache, not the job container. Most of the per-job CA/proxy env can
  eventually move into the builder image; keep it for now.
- **`haku-state` `.forgejo/actions/bazel-runner-setup/action.yml`**: replace "install bazelisk
  - `actions/cache` + trust CA" with "ensure the warm builder exists" (the `docker inspect ||
docker run` above).
- **`haku-state` `.forgejo/workflows/{bazel-ci,validate-state}.yaml`**: replace the in-job
  `bazel test/build …` steps with `docker exec haku-bazel-builder ci-build.sh …`. **Keep the
  credentialed push steps in the ephemeral job container** (see the isolation decision) — the
  builder produces the image tarball with `bazel build //ui:image`; the job container, which
  holds `REGISTRY_PUSH_TOKEN`, does the crane push of that tar.
- **new** `haku-state tools/ci/ci-build.sh` + a `Dockerfile` for the builder image.

### Gotcha: PVC node-affinity coupling

`local-path` PVCs are node-pinned. The runner Deployment is node-affinity'd to region `hil`
workers with `Recreate`, so the PV binds to whichever worker the pod first lands on; a
reschedule to a _different_ node strands the PVC (local-path can't migrate). For a regenerable
cache this is acceptable — delete the PVC, re-provision, eat one cold build. `seaweedfs` (RWX,
networked) would move but is far too slow for a Bazel cache. Recommend `local-path-ovh-hdd`
and accept the coupling.

## The isolation decision

This is the one call the operator has to make.

### Current model

Per `cluster/k8s/haku-ci/README.md` and `haku/console/docs/containment.md`, the runner
executes **Haku-authored** build steps (the workflow + Dockerfile in `haku-state`). A
prompt-injected Haku could author a hostile build, so the runner is contained to roughly
Haku's existing sandbox blast radius: own namespace with **no Haku RBAC**, egress-fenced, no
cluster creds (`automountServiceAccountToken: false`), pinned off the control planes,
`capacity: 1`. Crucially, a **fresh job container per job** means each build starts from a
clean ephemeral filesystem and nothing bleeds between jobs.

### What Tier 2 changes

It removes the "fresh container per job" property. The warm builder is a **long-lived
container with a persistent workspace + output base, shared across every PR's build**:

- **Cross-PR state bleed.** PR A's build actions (genrules, tests — arbitrary code) run in the
  same container and write to the same output base / disk cache that PR B then builds against.
- **Persistent foothold.** A compromised build could leave a process running in the builder
  that survives into later jobs (vs. today's per-job teardown).
- **Resource / DoS.** A build could fill the cache PVC or pin the warm server's RAM.

### What stays contained (unchanged)

- Still the `haku-ci` namespace, no Haku RBAC, egress-fenced, no cluster creds, off the
  control planes. The builder reaches nothing the job container couldn't — same egress door,
  same in-cluster-only perimeter.
- The new exposure is strictly **cross-PR, within that same perimeter** — not a widening of
  the outer boundary.
- **Push credentials never enter the builder** (design refinement above): credentialed steps
  stay in the ephemeral job container, so a hostile build in the builder does not gain
  `REGISTRY_PUSH_TOKEN` or the git-push creds.
- `haku-ci` is **single-tenant**: only Haku/operator author PRs. The threat is therefore
  "prompt-injected Haku poisons a later Haku build," not a public multi-tenant CI.

### Mitigations

- `git clean -fdx` (or a fresh `git worktree` per sha) so workspace files don't leak between
  PRs.
- Keep credentialed (push) steps in the ephemeral job, not the builder.
- Periodic builder bounce (recreate daily / every N builds) to cap foothold longevity and
  cache drift.
- Resource limits on dind and a cache-size cap — `--disk_cache` has **no auto-eviction**, so
  add a periodic prune (or adopt an in-cluster `bazel-remote`, which does LRU; see
  alternatives).
- The disk cache is content-addressed (keyed by action hash), so poisoning a _specific_
  downstream build is hard; a paranoid option is a read-only disk cache, but that kills the
  win.

### The call

Because `haku-ci` is single-tenant and already treated as adversarial-but-contained, the
marginal risk is "prompt-injected Haku poisons a later Haku build" — bounded to the same
perimeter, no outer-boundary widening, and mitigatable (clean checkout, creds stay in the job,
periodic bounce). **Recommendation: acceptable _with_ the mitigations, given single-tenancy;
revisit if `haku-ci` ever runs non-Haku-authored code.**

## Alternatives considered

- **Tier 1 — bind-mount the disk cache only, keep the fresh job container.** Warms disk + repo
  cache but not the server (no analysis reuse) → `bazel` ~60–150 s. Zero isolation cost; full
  per-job isolation preserved. Lower ceiling, but a good substrate Tier 2 builds on, and it
  can ship independently and immediately. **Landed** in `cluster/k8s/haku-ci/` (`pvc.yaml` +
  the `-v /bazel-cache:/root/.cache` job-container mount in `config.yaml`): a
  `local-path-ovh-hdd` PVC mounted into dind and bind-mounted onto every job container's
  `~/.cache`, so the output base + `--disk_cache` + repo cache persist across job containers.
- **Tier 3 — in-cluster `bazel-remote` gRPC cache.** Persistent, LRU-evicting,
  concurrency-safe, survives node moves; honors source-in-cluster (only content-addressed CAS
  blobs go to it, never source). Complements Tier 2 (remote cache for execution, warm server
  for analysis) but gives no warm RAM on its own.
- **Fix `cache: enabled` and keep `actions/cache`.** The tar → upload → download → untar tax
  rivals the build itself for a multi-GB Bazel cache. Strictly worse than a bind-mount; not
  recommended.

## Rollout

1. Land the **Tier 1 substrate** (PVC + cache mount) — safe, immediate, no isolation change.
   Ships independently.
2. Build and publish the **builder image** to the in-cluster registry.
3. Add lazy-create + `docker exec` dispatch in a **non-gating** workflow first; measure warm
   vs. cold.
4. Flip **`validate-state` first** (smallest graph, lowest risk), then `bazel-ci` test/build
   — keeping the push in the job container.
5. Add the periodic builder bounce + cache GC.

**Rollback:** revert the workflow steps to in-job `bazel`; delete the builder container + PVC.
The output base and disk cache are regenerable.

## Validation

- Confirm act_runner does **not** reap the named `haku-bazel-builder` container between jobs
  (it should only remove the job containers it created).
- Measure first (cold) vs. second (warm) wall time; confirm "Starting local Bazel server" is
  **absent** on warm runs and the `--profile` trace shows ~0 analysis time on a no-op diff.
- Confirm push credentials never appear in the builder's environment.

## Open questions

- **Builder-image build path** (chicken-and-egg): a plain `docker build` bootstrap step, or
  ducktape's `oci_image` pipeline → in-cluster registry?
- **Cache GC policy** — `--disk_cache` has no auto-eviction; prune cadence vs. adopting
  `bazel-remote`.
- **Bounce cadence** for the builder (foothold longevity vs. warmth).
- **Server memory budget** — start at ~4 GiB inside the dind limit, tune from the profile.
