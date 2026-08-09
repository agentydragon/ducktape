# `small` tests timing out on CI RBE (2026-08-03)

Two augur PRs went red on `bazel-ci / Test & Build` with a single TIMEOUT each, on
targets neither PR touched:

| Run              | Victim                                         | Result        |
| ---------------- | ---------------------------------------------- | ------------- |
| #3696 `eebc9ee0` | `//finance/augur/sim:test_property_stakes_e2e` | TIMEOUT 64.1s |
| #3697 `62cb3be7` | `//finance/augur/fit:private_equity_test`      | TIMEOUT 60.0s |
| #3697 re-run     | `//finance/augur/fit:private_equity_test`      | TIMEOUT 60.0s |

Both are `size = "small"` → a 60s Bazel test timeout.

## What it is not

**Not a hang**, which is the default reading AGENTS.md prescribes for a timeout. Both
victims complete well inside the budget on demand, and each passed in the very run where
the other died.

**Not queueing, and not a degraded fleet.** The first #3697 run _looked_ like fleet
trouble — one execution failed with `prepare runner filesystem: Error pulling container:
context deadline exceeded`, and the timed-out action showed a 124s gap between
`workerStartTimestamp` and `inputFetchStartTimestamp`. That made contention an attractive
story. The **re-run refutes it**: `Executed 1 out of 56 tests` (everything else served
from cache), so the target ran alone on an idle fleet, and its execution metadata is clean:

```text
queuedTimestamp              +0.00s
workerStartTimestamp         +0.07s     <- no queueing
inputFetchStartTimestamp     +0.19s     <- warm VM, no container pull
inputFetchCompletedTimestamp +0.43s
executionStartTimestamp      +0.43s
executionCompletedTimestamp +60.43s     <- exactly 60.001s of execution, then killed
```

**Not caused by either PR.** `//finance/augur/fit:private_equity_test` passes on the
#3697 branch with `--nocache_test_results` (14.4s, invocation
`8aec9552-c7a2-4bd0-84cc-ccb5e2d6c934`), and the model tests the PRs actually change were
all cache hits in the failing runs.

## What it is

**The CI worker image is materially slower than the default RBE image**, and these tests
were sized without that margin.

`.github/actions/bb-remote/action.yml` pins the worker image from
`devinfra/image_pins.json` and `bazel-ci.yml` passes it through as
`--remote_default_exec_properties=container-image=docker://$RBE_IMAGE`. An ad-hoc `bbr`
run does **not** — it gets BuildBuddy's default executor image. Same target, same branch,
same commit, only the image differs:

| Executor image                             | `fit:private_equity_test`                          |
| ------------------------------------------ | -------------------------------------------------- |
| BuildBuddy default                         | **14.4s** (`8aec9552-c7a2-4bd0-84cc-ccb5e2d6c934`) |
| `ghcr.io/agentydragon/rbe-worker` (CI pin) | **26.4s** (`9b3b3e89-3037-4df5-aa3a-8f1fc07574d3`) |

~1.8x, reproducible. That leaves a `small` (60s) numerical test with ~2.3x headroom on the
image CI actually uses, and these are numpyro/JAX tests whose runtime is inherently
variable. The victim differs run to run because several tests sit near the same line.

**It reproduces on `devel` with no PR changes at all.** `bbr test //finance/augur/... --nocache_test_results`
on the CI image, from a clean `devel` checkout (invocation `46f30d92-bdf4-4333-aba4-d8fdf14a281b`),
timed out on a _third_ distinct set:

```text
//finance/augur/fit:test_dilution_prior   TIMEOUT 62.6s
//finance/augur/model:gbm_test            TIMEOUT 69.2s
//finance/augur/sim:scan_test             TIMEOUT 60.0s
```

Three runs, three different victims, none of them the tests any PR touched. This is a
property of the suite, not of any change.

## Why so many tests sit near the line

`devinfra/python/defs.bzl` defines `py_test(name, size = "small", ...)` — the repo-wide
default is **60 seconds**, not Bazel's own 300s `medium` default. Every test that doesn't
name a size gets the tightest budget Bazel offers.

That default is fine for the genuinely small tests (`sim:tax_test` 5.2s,
`sim:tensor_fifo_test` 10.6s) and wrong for anything importing jax/numpyro, which pays
~15-25s of interpreter and import cost before executing a single assertion. On the CI
image roughly half the augur suite measures 24-70s against that 60s budget.

## The fix applied here

Explicit `size = "medium"` on the augur tests measuring **≥24s** on the CI image. The
threshold is derived rather than picked: `sim:test_property_stakes_e2e` measures 25.7s yet
died at 64s in CI — a 2.5x excursion — so 60/2.5 ≈ 24s is the point below which `small` is
actually safe.

Deliberately _not_ done: raising the macro's default from `small` to `medium`. That would
fix this class repo-wide in one line, but it changes test-sizing semantics for every
package and weakens the "small tests should be small" signal. It is the better long-term
answer if this keeps recurring outside augur — flagged, not taken unilaterally.

**Consequence for verification workflow:** a green `bbr test` locally does not imply the
same test is green in CI, because the executor image differs. That is the same class of
gap as "a green `bbr test` does not imply green lint".

## Re-measured 2026-08-09, and the list changed

Splitting this out of #3698 meant re-running the evidence, and it no longer reproduces. Same
pinned image (`ghcr.io/agentydragon/rbe-worker@sha256:daa5830d…`), same `--nocache_test_results`:

| target                         |    2026-08-03 | 2026-08-09 |
| ------------------------------ | ------------: | ---------: |
| `fit:private_equity_test`      |         26.4s |   **9.7s** |
| `model:gbm_test`               | TIMEOUT 69.2s |  **10.4s** |
| `fit:test_dilution_prior`      | TIMEOUT 62.6s |   **9.6s** |
| `sim:scan_test`                | TIMEOUT 60.0s |  **20.0s** |
| `sim:test_property_stakes_e2e` | TIMEOUT 64.1s |  **14.4s** |

2.7-6x faster, and every target sized above now runs under 30s. The cause is not established —
the worker image may have been rebuilt in the intervening six days, which would also quietly
answer the open question below. **The `medium` sizes are kept anyway**: the slowdown was never
explained, so it can return, and an oversized budget costs nothing but a Bazel warning while an
undersized one costs a red CI run on an unrelated PR.

What the re-measurement did change is **which** tests sit near the line. A full-suite run
(`//finance/augur/...`, CI image, no cache) puts four `small` tests in the top of the
distribution, and **none of them were in #3698's list**:

| target                           | measured | was                     |
| -------------------------------- | -------: | ----------------------- |
| `sim:test_target_allocation_e2e` |    42.4s | `small` (macro default) |
| `api:test_export_schema`         |    33.4s | `small` (macro default) |
| `sim:target_allocation_test`     |    30.6s | explicit `small`        |
| `sim:allocation_test`            |    30.5s | explicit `small`        |

At 42.4s against a 60s budget, `test_target_allocation_e2e` has 1.4x of headroom — tighter than
anything the original investigation flagged. (It grew a case in #3868, which is part of why.)
These four are sized here too.

`api:test_export_schema` is worth calling out separately: #3698 sized `api:export_schema_test`,
and there are **two py_test targets on the same `test_export_schema.py` source**. It sized one of
the pair and missed the twin, which is the one that actually measures slow.

## Why the usual bisect recipe was unavailable

`bbapi target {history,stats,flakes}` return empty for this repo, so localizing this by
per-target history was not an option and the investigation above had to re-run CI and watch
instead. The cause is a BuildBuddy runner detail, independent of test sizing, and is chased
separately in <2026_08_buildbuddy_target_tracking.md>.

## Open question

Why the pinned worker image is 1.8x slower for numerical Python is **not** established.
Nothing in the repo sets `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, or
`XLA_FLAGS`, so a BLAS/threading difference between the two images is the obvious first
place to look — but that is a hypothesis, not a finding. Worth measuring before any
attempt to close the gap, rather than sizing tests around it forever.
