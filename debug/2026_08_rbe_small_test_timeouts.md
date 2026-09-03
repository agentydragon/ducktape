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
instead. The cause is a BuildBuddy runner detail, independent of test sizing: `bb remote
--script` runs are classified as hosted bazel, which turns target tracking off — the
mechanism, and the push-only re-enable, live in the target-tracking comment in
<../devinfra/ci/bazel_ci.sh>.

## Open question

Why the pinned worker image is 1.8x slower for numerical Python is **not** established.
Nothing in the repo sets `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, or
`XLA_FLAGS`, so a BLAS/threading difference between the two images is the obvious first
place to look — but that is a hypothesis, not a finding. Worth measuring before any
attempt to close the gap, rather than sizing tests around it forever.

## 2026-09-03: it now recurs outside augur, and the mechanism is visible

The condition this note set for the repo-wide fix — "if this keeps recurring outside augur" —
is met. Two packages that share nothing with augur, and nothing with each other, flake the
same way. All figures are `bbr test --nocache_test_results --runs_per_test=10`.

| target                                | TIMEOUTs | note                    |
| ------------------------------------- | -------: | ----------------------- |
| `//x/agentplane/egress:test_proxy`    |     4/10 | 5/10 when run alone     |
| `//x/agentplane/egress:test_sidecar`  |     4/10 |                         |
| `//x/agentplane/egress:test_policy`   |     3/10 | no I/O at all           |
| `//x/agentplane/egress:test_admin`    |     3/10 |                         |
| `//x/agentplane/egress:test_identity` |     3/10 |                         |
| `//x/agentplane/egress:test_upstream` |     2/10 |                         |
| `//util:test_sqlalchemy_types`        |     2/10 | control, unrelated tree |
| `//util:test_image_tag`               |     1/10 | control, unrelated tree |
| `//util/bazel:test_workspace`         |     1/10 | control, unrelated tree |

The controls are what make this general: they were run precisely to falsify "the egress
package is special", and they flake too. `//x/agentplane/egress:test_policy` rules out the
other tempting story — it is pure synchronous logic, no `async def`, no aiohttp, no fake API
server, nothing that can block — and it still dies at 64.3s.

**It is not confined to the CI image either.** This note established that `bbr` gets
BuildBuddy's default executor while CI pins `ghcr.io/agentydragon/rbe-worker`, and that the
pin was 1.8x slower. Every row above is on the _default_ image, while the failure that
started this round (`test_policy` TIMEOUT 65.6s on #5469) was on the pinned one. Both images.

**Where the time goes, measured rather than inferred.** An earlier draft of this section blamed
interpreter and import cost, carrying over the augur story. That is wrong here, and the measurement
says so. With `PYTHONPROFILEIMPORTTIME=1`, the whole import tree costs **1.5s** (`pytest_bazel`
cumulative 1.540s, the root, so it bounds everything under it); the heaviest entries are pytest's own
machinery, and `kubernetes_asyncio` does not reach the top eighteen. A green `test_proxy` is 1.55s of
pytest for 11 tests, a green `test_informer` 0.35s. Call it three seconds of Python.

The cost is the execution platform. BuildBuddy's own per-execution timings, same invocation:

| phase                                | `//util:test_image_tag` | `//x/agentplane/egress:test_policy` |
| ------------------------------------ | ----------------------: | ----------------------------------: |
| queued -> worker                     |                   0.06s |                               0.09s |
| worker -> input fetch (VM/container) |               **4.43s** |                         **252.83s** |
| input fetch                          |                   0.85s |                               9.62s |
| execution                            |              **40.69s** |           60.96s, killed at the cap |

`test_image_tag` is a trivial test: ~3s of Python inside a 40s execution, behind 4s of VM preparation.
`test_policy` waited over four minutes for an executor to prepare a filesystem. So `size = "small"`
is not measuring test work at all; it is measuring how long an executor takes to hand a process a
filesystem, and 60s sits inside that variance. A timed-out log ending at `pytest_bazel`'s
`Running pytest.main with:` line without reaching `test session starts` is consistent with this: it is
where a starved process happens to be when the cap fires, not work that costs a minute.

**The cause, from the executors' own I/O counters.** `executedActionMetadata.ioStats` settles it.
Five sequential runs of `//util:test_image_tag`, alone on an idle fleet, all landing on the same warm
worker:

| run                        |      1 |      2 |      3 |      4 |      5 |
| -------------------------- | -----: | -----: | -----: | -----: | -----: |
| VM prep                    |  0.01s |  0.06s |  0.00s |      - |      - |
| Bazel `inputFetch`         |  0.21s |  0.43s |  0.36s |      - |      - |
| `fileDownloadDurationUsec` | 22.50s | 18.12s | 24.40s | 13.69s | 15.10s |
| `cpuNanos`                 |  3.67s |  4.18s |  3.94s |  3.58s |  4.32s |

`fileDownloadCount` and `fileDownloadSizeBytes` are **zero** in every one. Zero files, zero bytes, and
fourteen to twenty-four seconds charged to the input-filesystem layer, against under four and a half
seconds of CPU. The execution phase is almost exactly that I/O figure: the action is not computing,
it is waiting on its own filesystem. Note it does not warm up across runs on one worker.

The executors serve action inputs lazily. Bazel's `inputFetch` phase is a fifth of a second because
nothing is prefetched, so the transfer cost reappears inside execution as latency. That is why
`PYTHONPROFILEIMPORTTIME` sees only 1.5s: it measures CPU spent in the import, not the stalls around
the file opens.

**It is not Python.** A `rust_test` (`//loom/wayback/cache:wayback_cache_test`), same fleet, two
executions of the identical action: `EXEC 2.66s / ioWait 3.06s / cpu 1.27s`, and
`EXEC 32.33s / ioWait 32.35s / cpu 0.99s`. `EXEC` tracks `ioWait` and CPU is noise, in a language with
no venv and no site-packages. Python tests are the usual victims only because their runfiles tree is
the largest, so they touch the layer most; nothing about `rules_python` or `bootstrap_impl=script` is
at fault, and neither is any test in this repo.

**Caveat on the earlier phase numbers in this entry** (the 252.83s VM prep)**Verified fix.** With the `py_test` default raised to `medium`, the same ten targets over ten runs
each: **100 of 100 passed, no timeouts**, against 2-4 in 10 failing per target before. Several passing
runs took 90-101s — above `small`'s cap outright, so those could not have passed under it.

**Sized where it hits, and the default stays `small` (Rai).** The seven
`//x/agentplane/egress` targets carry `size = "medium"` because they are the ones observed failing:
one on CI, the rest at 2-4 in 10 locally. The `devinfra/python/defs.bzl` default is unchanged, so a
test that has not hit this keeps the 60s budget and keeps meaning something by it. Raising the
default was drafted and rejected: it would have re-sized every Python test in the repo for a
platform property, and the August entry's objection -- that it weakens the "small tests should be
small" signal -- is not answered by any evidence here.

The three `//util` controls in the table above are deliberately **not** sized. They are the
falsification test, not a symptom: they failed only under 30-70 concurrent actions this
investigation induced itself. Size them if they ever fail a real run.

**Verified.** Ten targets over ten runs each, with these sizes: **100 of 100 passed, no timeouts**,
against 2-4 in 10 failing per target before. Several passing runs took 90-101s -- above `small`'s cap
outright, so they could not have passed under it.

**This is a stopgap over a platform property no BUILD file can reach.** Nothing here makes a test
faster; it widens a budget so 14-32s of executor I/O latency stops failing unrelated PRs. The real
fix is executor-side -- lazy input fetching on the BuildBuddy pool -- and is worth raising with them
with the counters above, since 0 files and 0 bytes taking 24 seconds is theirs to explain. This also
answers the August open question: the pinned image was never the mechanism.

### Not the cause, so nobody re-walks these

- **An aiohttp session leak.** `test_proxy` did have one (a `ClientResponse` returned from
  inside its `async with ClientSession()`), fixed in #5471. It made no difference: 5/10 still
  timed out. Worth having as a correctness fix, worthless as an explanation.
- **The egress package's import closure.** Its `conftest.py` pulls `kubernetes_asyncio` into
  every target including pure-logic ones, which looked like a fine culprit until the `util`
  controls — which import none of it — flaked as well.
- **A genuine wedge, in one case only.** `//x/agentplane/egress:test_informer` really did hang
  on a 60s watch race (#5469), unrelated to any of the above; its file now runs in 0.35s. It is
  named here because it sat inside this same symptom and will otherwise look like more evidence
  for a story it does not belong to.
