# Runner snapshot-reuse stats

How often do our BuildBuddy Firecracker runners **resume from a snapshot**
(warm) vs **cold-start a fresh VM** and redo the full git clone + external-repo
fetch work?

`runner_recycle_stats.py` answers this empirically from BuildBuddy history.

## Background

Every `bbr`/CI run dispatches a **ci_runner Firecracker VM** — the
`HOSTED_BAZEL` "remote …" invocation, configured in <../../bbr.json>:

```json
"recycle-runner": "true",
"remote-snapshot-save-policy": "always",
"snapshot-read-policy": "newest"
```

Inside that VM, `bazel …` runs and produces the inner (child) invocation. The VM
is what snapshots/recycles; the inner bazel build is a separate concern.

## The signal

There's no clean structured field for warm-vs-cold (`bbapi execution` is
currently broken on a proto field). The **runner's first console lines** are
authoritative:

| Verdict  | Console header                                                                            | What happens                                                                                                                                                                                                |
| -------- | ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **warm** | `Syncing existing repo...` → shallow `git fetch --depth=1` + `git clean` + `git checkout` | repo and Bazel `output-base` (`/home/buildbuddy/workspace/output-base`, outside the git-cleaned `repo-root`) survive → external-repo fetches + analysis cache reused; inner build shows `0 packages loaded` |
| **cold** | `Cloning ...`                                                                             | full clone + re-fetch every external repo + reload all packages — "redoing all the local repo fetch work"                                                                                                   |

`bb view <runner-invocation-id>` streams that console.

## What triggers a cold start

Cold runs are gated on snapshot-key invalidation, primarily:

1. **Runner image digest bump** — `container_image` in <../../bbr.json>
   (`ghcr.io/agentydragon/rbe-worker@sha256:…`). A new digest starts a fresh
   snapshot lineage, so the first runs after a roll are cold.
2. **Snapshot-cache eviction** in BuildBuddy.

Because `remote-snapshot-save-policy=always` persists the snapshot to the
**remote** cache (not just the local executor), even a freshly-scaled executor
after a long idle gap restores the snapshot instead of cloning. Idle time alone
does **not** force a cold start.

## Usage

```bash
# Full sample over the densest recent window
devinfra/x/runner_recycle_stats/runner_recycle_stats.py --count 600

# Wider window (API pages by recency; larger N == more hours)
devinfra/x/runner_recycle_stats/runner_recycle_stats.py --count 8000

# Only classify the cold-start candidates: first runner after each >10min idle gap
devinfra/x/runner_recycle_stats/runner_recycle_stats.py --count 8000 --gaps-only
```

Needs `BUILDBUDDY_API_KEY` (session hook / Nix devshell) and `bb` + `bbapi` on
PATH.

## Findings (2026-06-08, ~40h window)

Pulled 8000 invocations spanning 39.8h (2026-06-07 07:46 → 2026-06-08 23:36
UTC); 3930 were runner invocations.

| Sample                                                                       | warm | cold |
| ---------------------------------------------------------------------------- | ---- | ---- |
| All 296 runners in the densest 2.3h window                                   | 296  | 0    |
| All 23 "first after a >10min idle gap" (incl. a 9.6h and 6.8h overnight gap) | 23   | 0    |

**100% warm reuse, zero cold clones.** Even the first job after a ~10h overnight
idle was a warm snapshot resume. The runner image digest was last bumped
**2026-05-21** (18 days before the sample), so nothing in the window triggered a
relineage — consistent with uniform warmth. To actually observe a cold start,
sample the invocations in the minutes right after an rbe-worker image roll.

## GitHub Actions as an alternative source

CI runs `bbr` via `.github/actions/bb-remote`, so the same
`Syncing existing repo` / `Cloning` header is streamed into the `bazel-ci` job
log (`gh run view --log`). It's strictly redundant with BuildBuddy and noisier
to parse (interleaved GHA wrapper output, shorter log retention, no invocation
for fork PRs). Prefer BuildBuddy; reach for GHA logs only for cold-start
forensics on a specific CI run where you also want the surrounding GH context.
