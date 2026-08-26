# CI slowness: runner-slot starvation, not Bazel

Measured 2026-08-26, ~11:00–14:15 UTC, on `agentydragon/ducktape` (public repo).

## Verdict

Bazel and BuildBuddy are fast. CI is slow because every `devel` merge fans out to
**95 GitHub Actions jobs**, and those jobs spend ~10x longer waiting for a runner slot
than they spend working. In the sampled window, **27 `devel` push runs produced zero
green results** (18 cancelled, 5 failed, 4 still queued).

## Where the time goes

Run [32967283495](https://github.com/agentydragon/ducktape/actions/runs/32967283495)
(`devel` push, representative), 90 of 94 jobs sampled:

| Measure                        | Value                      |
| ------------------------------ | -------------------------- |
| Jobs                           | 90                         |
| Summed job runtime             | 101 min (median job 65 s)  |
| Summed queue time              | **1,542 min**              |
| Wall clock                     | 44.2 min                   |
| Effective parallelism achieved | **2.3 concurrent runners** |
| Max concurrent observed        | 13 (partial sample)        |

Median job queue time 17.9 min against a median job runtime of 65 s. With 20 free slots
the same work drains in ~5 min.

## The fan-out is ~99% no-op

Across 96 sampled `push-images` matrix jobs, **exactly 1 pushed an image**. The other 95
spent ~87 s each discovering the digest was unchanged.

- 39% of each push job is fixed overhead: `actions/checkout` (14 s) + `setup-nix-devtools`
  (17 s).
- 42 matrix jobs/merge ≈ 61 min of runner time, ~24 min of it pure setup.
- `release` adds another 45–50 jobs/merge (mean 56 s), same build → content-hash → skip
  shape.
- 32 of the 42 push jobs re-run `bb remote test <target>` that `bazel-ci` already ran on
  the same commit.

## Bazel/BuildBuddy is not the bottleneck and is not throttled

- Median BuildBuddy invocation: **5 s** (p90 14.8 s), n=200.
- Action cache hit rate: **97.1%** (10,906 hits / 324 misses).
- The `bazel-ci` job itself — full `//...` test + build + lint — is **1.5 min** of runner
  time.
- No quota or rate-limit errors in any sampled log.

## But BuildBuddy egress is real, and the fan-out causes it

- **~1,114 invocations/hour**, ~**100 GB/hour** wire-transferred from CAS.
- 9 of 200 invocations account for 96% of that egress (17.7 of 18.4 GB) — and **all nine
  had zero action-cache misses**. They are cold `bb remote` runner VMs re-materializing
  1–3 GB of state to do no work.
- The other 191 invocations transferred a median of 0 MB.

Cold-start count scales with the number of separate `bb remote` invocations, i.e. with the
fan-out (~92 per merge). This is the likely cause of BuildBuddy's usage email; it is a
side effect of the same root cause, not an independent problem.

## Load context

`devel` merge rate: 55–116 commits/day over the past week (116 on 2026-08-26 by 14:15 UTC).
At ~1.8 job-hours per merge, average demand is roughly a quarter of a 20-slot pool — the
problem is burstiness, not sustained overload. 95 jobs arrive at once, several merges
overlap, and the queue never drains between them.

## Separate real bug

`push-images / Push props agent images` fails on every `devel` push:

```text
crane digest props-registry.allegedly.works/critic:latest failed (exit 1)
GET https://props-registry.allegedly.works/v2/: unexpected status code 404 Not Found
```

Unrelated to throughput; it is one of the persistent red marks on `devel`.

## Fixes, highest leverage first

1. **Two-phase the `push-images` and `release` matrices.** One job computes all digests /
   content hashes in a single `bb remote build`, diffs against the registry and releases,
   and emits a dynamic `matrix.include` of only what changed. Takes ~95 jobs/merge to ~5,
   and ~92 `bb remote` calls/merge to ~5 (which is where the CAS egress comes from).
2. **Debounce publishing.** Give `push-images` + `release` their own
   `concurrency: { group: publish-devel, cancel-in-progress: true }`, or move them to a
   15-minute schedule. A merge burst then collapses to one publish. A few lines of YAML.
3. **Cut the fixed per-job overhead** (~31 s × 92 jobs ≈ 48 min/merge). Publish jobs need
   `bb`, `crane`, `jq` — not the whole nix devshell. Cache the nix store or fetch `bb`
   directly. Largely moot after (1); do (1) first.
4. **Stop double-testing.** Once (1) lands, gate the small changed set on `bazel-ci`'s
   result instead of re-running per-image tests.
5. **`devel` never goes green.** `bazel-ci`'s `cancel-in-progress` on
   `bazel-ci-refs/heads/devel` plus a ~45-min queue and merges every ~7 min means the full
   sweep essentially never completes. (1)–(3) should make the sweep finish in ~5 min and
   resolve this; otherwise drop `cancel-in-progress` for `devel`.

Paying BuildBuddy would not fix this — the constraint is GitHub runner slots. GitHub Team
(60 concurrent jobs vs 20 on Free) would be a 3x throughput increase, but (1) shrinks the
fan-out ~20x for free. Do (1) before spending anything.
