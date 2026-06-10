# CI Firecracker / Bazel Analysis Cache Profile

Date: 2026-06-10

## Question

Recent notes concluded that CI/`bbr` Firecracker runner invocations are reusing snapshots.
That may be true for the outer runner VM while still not proving that the inner Bazel server's
analysis cache survives CI runs. This investigation separates:

- outer `bb remote` runner warm/cold state (`Syncing existing repo...` vs `Cloning ...`);
- inner Bazel package loading / target configuration / analysis timing;
- action-cache and execution timing from BuildBuddy.

## Prior Claims To Check

- `x/firecracker_workflow/README.md`: warm snapshot recycling should preserve full VM memory,
  including a running Bazel server and in-memory analysis cache. A fully warm repeat showed
  `0 packages loaded, 0 targets configured`.
- `devinfra/x/runner_recycle_stats/README.md`: sampled runner logs showed 100% warm outer
  runner reuse over a recent window, and the note infers that external-repo fetches and analysis
  cache are reused.

The main suspected hole: the newer runner-recycle analysis classifies only the outer runner
header. It does not independently measure inner Bazel analysis work in CI.

## Work Log

- Created worktree `/tmp/ducktape-ci-firecracker-profile` on branch
  `codex/ci-firecracker-profile` from `origin/devel` at `f6662c850`.
- Read prior notes:
  - `x/firecracker_workflow/README.md`
  - `debug/bazel_ci_analysis_cache.md`
  - `debug/bb_remote_default_branch_cache_warning.md`
  - `devinfra/x/runner_recycle_stats/README.md`
  - `devinfra/x/runner_recycle_stats/runner_recycle_stats.py`
- Confirmed CI uses `.github/actions/bb-remote/action.yml`, which passes:
  - `workload-isolation-type=firecracker`
  - `init-dockerd=true`
  - `recycle-runner=true`
  - `remote-snapshot-save-policy=always`
  - `snapshot-read-policy=newest`
- Opened PR #2013 from this branch to run instrumented CI against the real GitHub Actions
  -> BuildBuddy path.

## Evidence Tables

### Recent CI Sample

Sampled recent `bazel-ci / Test & Build` GitHub Actions jobs on 2026-06-10. All four
outer runner logs contained `Syncing existing repo...`, so the runner workspace/rootfs
path is warm by the existing `runner_recycle_stats` classifier.

| GH run        | GH job        | result  | commit        | outer runner invocation                | child Bazel invocations                                                                   |
| ------------- | ------------- | ------- | ------------- | -------------------------------------- | ----------------------------------------------------------------------------------------- |
| `27302927129` | `80653198772` | success | `6d460847...` | `4c6a40ea-286c-44d1-9d88-d03236e52365` | test `807b8b62-73ee-4b88-a5a9-4deb28c3312a`, build `a319a101-db09-44bf-9dc0-d181b581c150` |
| `27303834911` | `80656268692` | success | `7daf37a...`  | `20f633a7-6ac6-4131-9b2e-b5d0d98caf2b` | test `b479e30b-5c46-4837-941f-449f1e2bca5c`, build `c1ee99c2-ad30-41e9-ae77-3befbbc60400` |
| `27304748140` | `80659460268` | success | `7daf37a...`  | `46e65377-9b48-4585-aa72-08781e421abe` | test `8f5235d5-3a5a-433d-883c-62cee7ac3409`, build `835339f9-a89f-461d-9797-8dd18d39791e` |
| `27305863277` | `80663371184` | failure | PR head       | `77e96787-c5a0-495f-a447-6e74d11ae0ce` | test `32b6919b-3ce3-4737-b9a4-222d3b1f15c1`                                               |

The failure in `27305863277` was a real build failure, not a profiling failure:
`//tana/litellm_proxy:provider` failed its mypy aspect, and
`//cluster/k8s/litellm/tana:test_generate_tana_litellm` also failed.

### BuildBuddy Tool Logs

`command.profile.gz` is not a test artifact. It is a BES `BuildToolLogs` entry on the
invocation, next to inline logs named `elapsed time`, `critical path`, and `process stats`.
The initial `bbapi artifact` model hid this domain distinction, so this branch adds:

```bash
bbapi tool-log list <invocation-id>
bbapi tool-log cat <invocation-id> "critical path"
bbapi tool-log download <runner-invocation-id> command.profile.gz --all -o profiles/
```

This command auto-resolves a workflow/runner invocation to child Bazel invocations, matching
the existing `target`/`artifact` behavior without inventing fake artifact labels.

### Phase Profile From `command.profile.gz`

Wall-clock markers from Bazel's Chrome trace profile. `load/analyze/exec marker span` is the
Bazel marker named `Load, analyze dependencies and build artifacts` through `Complete build`;
Bazel 8/Skymeld does not split this cleanly into separate analysis-only and execution-only
wall spans. `pre-action after load` is the wall time from that marker to the first
`action processing` event, which is a practical upper bound on non-action work after the
load/analyze marker for these warm samples.

| run           | role  | child      | console progress | elapsed | setup/diff before target eval | eval to load marker | load/analyze/exec marker span | pre-action after load | action span | action events | critical path | process stats                                                                       |
| ------------- | ----- | ---------- | ---------------- | ------: | ----------------------------: | ------------------: | ----------------------------: | --------------------: | ----------: | ------------: | ------------: | ----------------------------------------------------------------------------------- |
| `27302927129` | test  | `807b8b62` | `1 pkg / 0 cfg`  |   66.5s |                        3.830s |              0.035s |                       62.613s |                0.694s |     61.742s |            33 |        55.27s | 33 processes: 4658 action cache hit, 18 remote cache hit, 13 internal, 2 remote     |
| `27302927129` | build | `a319a101` | `0 pkg / 0 cfg`  |    9.1s |                        4.346s |              0.026s |                        4.669s |                0.753s |      0.140s |             1 |         1.22s | 1 process: 147 action cache hit, 1 internal                                         |
| `27303834911` | test  | `b479e30b` | `3 pkg / 0 cfg`  |   62.4s |                        2.765s |              0.053s |                       59.478s |                0.597s |     58.748s |           272 |        50.11s | 272 processes: 6739 action cache hit, 157 remote cache hit, 113 internal, 2 remote  |
| `27303834911` | build | `c1ee99c2` | `0 pkg / 0 cfg`  |    8.8s |                        3.892s |              0.019s |                        4.922s |                0.666s |      0.050s |             1 |         0.12s | 1 process: 147 action cache hit, 1 internal                                         |
| `27304748140` | test  | `8f5235d5` | `0 pkg / 0 cfg`  |  157.3s |                        3.300s |              0.024s |                      153.960s |                0.253s |    153.527s |         16156 |       118.35s | 16156 processes: 1302 action cache hit, 16276 remote cache hit, 24 remote           |
| `27304748140` | build | `835339f9` | `0 pkg / 0 cfg`  |    7.3s |                        3.599s |              0.019s |                        3.672s |                0.665s |      2.273s |             2 |         0.07s | 2 processes: 192 action cache hit, 1 remote cache hit, 1 internal                   |
| `27305863277` | test  | `32b6919b` | `3 pkg / 0 cfg`  |  124.5s |                        3.237s |              0.050s |                      121.082s |                0.625s |    120.302s |           656 |       116.85s | 656 processes: 4781 action cache hit, 53 remote cache hit, 370 internal, 233 remote |

Detailed profile internals:

| run           | role  | child      | `handleDiffs` | `fsvc.getDirtyKeys` | `prepareForExecution` | `skyframeExecutor.evaluateBuildDriverKeys` | `evaluateTargetPatterns` |
| ------------- | ----- | ---------- | ------------: | ------------------: | --------------------: | -----------------------------------------: | -----------------------: |
| `27302927129` | test  | `807b8b62` |        3.564s |              3.163s |                0.655s |                                    61.780s |                   0.027s |
| `27302927129` | build | `a319a101` |        4.168s |              2.803s |                0.692s |                                     3.898s |                   0.019s |
| `27303834911` | test  | `b479e30b` |        2.448s |              2.421s |                0.558s |                                    58.779s |                   0.034s |
| `27303834911` | build | `c1ee99c2` |        3.754s |              2.381s |                0.624s |                                     4.235s |                   0.013s |
| `27304748140` | test  | `8f5235d5` |        3.184s |              2.404s |                0.205s |                                   153.561s |                   0.015s |
| `27304748140` | build | `835339f9` |        3.480s |              2.304s |                0.621s |                                     2.989s |                   0.013s |
| `27305863277` | test  | `32b6919b` |        2.892s |              2.852s |                0.583s |                                   120.335s |                   0.038s |

Interval-union view of action categories. These are wall-time coverage of intervals in the
profile, not summed per-action durations. Categories overlap: for example, remote cache checks
can run concurrently with remote execution.

| run           | role  | child      | action processing wall | remote cache check wall | remote exec wall | remote process wall |
| ------------- | ----- | ---------- | ---------------------: | ----------------------: | ---------------: | ------------------: |
| `27302927129` | test  | `807b8b62` |                58.045s |                  0.966s |          56.067s |             55.421s |
| `27302927129` | build | `a319a101` |                 0.140s |                  0.000s |           0.000s |              0.000s |
| `27303834911` | test  | `b479e30b` |                56.103s |                  4.061s |          49.751s |             49.224s |
| `27303834911` | build | `c1ee99c2` |                 0.050s |                  0.000s |           0.000s |              0.000s |
| `27304748140` | test  | `8f5235d5` |               153.516s |                 53.929s |         148.640s |            147.152s |
| `27304748140` | build | `835339f9` |                 0.093s |                  0.041s |           0.000s |              0.000s |
| `27305863277` | test  | `32b6919b` |               117.647s |                 13.958s |         116.066s |             78.168s |

Top actions by profile duration:

| run           | role | child      | slowest actions                                                                                                                                                                                        |
| ------------- | ---- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `27302927129` | test | `807b8b62` | 55.16s `Testing //devinfra/claude/claude_hook/container_e2e:test_container_e2e`; 41.98s `Testing //devinfra/claude/claude_hook:test_mailbox_delivery_e2e`                                              |
| `27303834911` | test | `b479e30b` | 50.04s `Testing //devinfra/claude/claude_hook/container_e2e:test_container_e2e`; 41.83s `Testing //devinfra/claude/claude_hook:test_mailbox_delivery_e2e`                                              |
| `27304748140` | test | `8f5235d5` | 118.10s `Testing //loom/gym:test_inspect_harness`; 69.96s `mypy //loom/gym:test_compare_runs`; 64.81s `Testing //devinfra/claude/claude_hook:test_mailbox_delivery_e2e`                                |
| `27305863277` | test | `32b6919b` | 83.79s `Testing //devinfra/js/debundle:pipeline_test`; 56.97s `Compiling Rust bin runtime_tdz_on_imported_class_test`; 53.33s `Testing //devinfra/claude/claude_hook/container_e2e:test_container_e2e` |

### BuildBuddy Invocation Metrics

BuildBuddy `targetConfiguredCount` is high even in apparently warm runs. In this sample it is
`21030` for the successful `//...` test/build children and `21162` for the failing PR head.
Interpret this as the configured graph size/result count, not direct evidence that those
targets were recomputed from scratch. The stronger recomputation indicators are:

- console `discarding analysis cache` warnings;
- package/target progress lines showing newly loaded/configured targets;
- profile time before first action;
- profile spans for target-pattern evaluation / package loading / non-action Skyframe work.

For these CI samples, GitHub logs did not contain `discarding analysis cache`; progress lines
showed `0-3 packages loaded` and `0 targets configured`; actions began within 0.25-0.75s after
the combined load/analyze/execute marker.

| run           | role  | child      | command | duration | `actionCount` | `targetConfiguredCount` |
| ------------- | ----- | ---------- | ------- | -------: | ------------: | ----------------------: |
| `27302927129` | test  | `807b8b62` | test    |    66.5s |            33 |                   21030 |
| `27302927129` | build | `a319a101` | build   |     9.1s |             1 |                   21030 |
| `27303834911` | test  | `b479e30b` | test    |    62.4s |           272 |                   21030 |
| `27303834911` | build | `c1ee99c2` | build   |     8.8s |             1 |                   21030 |
| `27304748140` | test  | `8f5235d5` | test    |   157.3s |         16156 |                   21030 |
| `27304748140` | build | `835339f9` | build   |     7.3s |             2 |                   21030 |
| `27305863277` | test  | `32b6919b` | test    |   124.5s |           656 |                   21162 |

### Self-Instrumented PR Run

PR #2013's first CI run used the initial inline runner probe. It produced a more interesting
case than the earlier samples:

- GitHub run `27310006267`, job `80677575140`, passed in about 2m2s.
- The runner log still said `Syncing existing repo...`.
- Before the first Bazel command, the probe saw a pre-existing Bazel server:
  - PID `11591`
  - start time `Wed Jun 10 22:10:07 2026`
  - age about 9m35s at the `before-test` probe
  - output base `/home/buildbuddy/workspace/output-base`
  - workspace `/home/buildbuddy/workspace/repo-root`
- The marker from the previous implementation was missing, because this was the first run
  with marker instrumentation.
- `boot_id` was `acc2ffc5-1fbe-443a-b33d-08f76b58c8be`; per the older Firecracker notes,
  `boot_id` is recorded but is not treated as decisive.

The first Bazel child in that run, `a2d71f3f-1c08-41fe-af1e-dd1a00e7488b`, showed:

- `Elapsed time: 57.345s`, `Critical Path: 48.85s`.
- `704 processes: 61408 action cache hit, 695 remote cache hit, 1 internal, 8 remote`.
- Console progress included:
  - `Analyzing: 3529 targets (1 packages loaded, 0 targets configured)`
  - `Analyzing: 3529 targets (1 packages loaded, 3437 targets configured)`
  - `Analyzing: 3529 targets (1 packages loaded, 297270 targets configured)`
- Profile phase summary:
  - launch: 0.813s
  - init: 2.415s
  - target pattern evaluation: 0.044s
  - interleaved loading/analysis/execution: 54.072s
  - finish: 0.021s
- The critical path was dominated by one remote test action:
  `Testing //devinfra/claude/claude_hook/container_e2e:test_container_e2e` at 48.85s.

The second Bazel child in the same runner, `9528ade3-d76c-42c4-a60f-2427f3da73dd`, was a
small `bazel build //...`:

- `Elapsed time: 7.982s`, `Critical Path: 0.97s`.
- Console progress showed `0 packages loaded, 0 targets configured`.
- Profile phase summary:
  - init: 3.414s
  - interleaved loading/analysis/execution: 4.548s
  - critical path: `OCI Image //devinfra/firecracker/vm_pod:image` at 968ms.

This is the strongest evidence so far that the old runner-recycle note was too strong:
we positively observed VM/Bazel-process reuse, while the first Bazel command still emitted
large configured-target progress. That does not by itself prove a cold analysis cache, because
Bazel 8/Skymeld interleaves analysis and execution and the profile critical path was remote
execution. It does mean outer runner reuse and even a pre-existing Bazel JVM are insufficient
evidence for "no reanalysis".

### Local Validation Caveat

While validating the new `bbapi tool-log` command locally:

- first local `bazelisk build //devinfra/buildbuddy_cli:bbapi --config=rbe` analyzed
  `1036 packages loaded, 42027 targets configured` and took 340s wall time;
- the immediate rebuild analyzed `0 packages loaded, 0 targets configured` and took 14s;
- then `bazelisk test //devinfra/buildbuddy_cli:bbapi_test --config=rbe` printed
  `WARNING: Build option --test_env has changed, discarding analysis cache` and configured
  `41904 targets`.

This confirms two things:

- a real analysis-cache invalidation is visibly different in logs;
- package count alone is not enough; a run can show few packages loaded while still
  reconfiguring many targets if options invalidate analysis.

That invalidation appeared in the local `bazelisk` sequence, not in the sampled CI `bb remote`
logs.

## Current Read

The prior runner-recycle analysis is right about the outer runner state for the sampled CI
window: the logs show warm runner workspaces (`Syncing existing repo...`). It overreaches if
it treats that header alone as proof that the inner Bazel analysis cache survived.

The current evidence is mixed:

- sampled recent devel/PR CI runs showed no `discarding analysis cache` warnings and mostly
  `0-3 packages loaded`, `0 targets configured`;
- the self-instrumented PR run positively found a pre-existing Bazel server before the first
  command, but still showed a large configured-target progress counter;
- the second `bazel build //...` child inside the same runner remains consistently tiny;
- in the profile, the long wall-clock critical path is still dominated by remote tests/actions,
  not a separable serial target-pattern or package-loading phase.

The residual time the user noticed is real, but in these samples it is primarily:

- 2.4-4.2s of file-diff / dirty-key scanning before target-pattern evaluation;
- remote action cache checking for thousands of actions in the large PR sample;
- a small number of long remote executions/tests/mypy actions.

This does not yet prove that the analysis cache is being dropped between Firecracker snapshots,
but it does disprove the simpler claim that Firecracker/Bazel-process reuse is enough to infer
no reanalysis. The next useful data is a structured per-run bundle showing raw procfs state,
previous local probe logs, Bazel server identity, and BuildBuddy tool profiles for consecutive
recycled runs.

## Instrumentation Added On This Branch

`.github/workflows/bazel-ci.yml` now runs `devinfra/ci/bb_runner_probe.py` from inside the
`bb remote` runner script:

- before the first Bazel command;
- after `bazel test`;
- after `bazel build`.

The probe writes structured JSONL to
`/home/buildbuddy/workspace/.ducktape-ci-vm-probe/current/probes.jsonl`, persists the completed
run under `latest/`, and reads `latest/probes.jsonl` at the next `before-test` probe. On exit it
creates `current/probe.tgz` and attempts to upload that tarball with `bb upload`; successful
uploads are printed as `CI_VM_PROBE_CAS digest=...`.

The tarball includes:

- `probes.jsonl`;
- raw current-run procfs snapshots under `proc/<phase>/...`;
- previous-run logs under `previous/probes.jsonl`;
- previous-run procfs snapshots under `previous/proc/...`, if the VM has a local `latest/`
  directory from an earlier run.

The JSON summary parses `/proc/<pid>/stat` only for compact CI log fields such as Bazel server
start time and age. The raw `/proc` files in the tarball are the source of truth.

Positive recycle signal to look for on a later CI run:

- `CI_VM_PROBE_SUMMARY phase=before-test previous_run_log=yes`;
- a pre-existing Bazel server in `CI_VM_PROBE_SERVER ...` before the first Bazel command;
- matching current raw procfs state plus previous local probe logs in the uploaded tarball.

`boot_id` is logged but should not be used as the decisive signal; prior Firecracker notes say
it can change on snapshot restore.

## Commands / Artifacts

Downloaded profiles live outside the repo at `/tmp/ci-firecracker-profiles`.

Useful commands:

```bash
bbapi tool-log list 8f5235d5-3a5a-433d-883c-62cee7ac3409
bbapi tool-log cat 8f5235d5-3a5a-433d-883c-62cee7ac3409 "critical path"
bbapi tool-log download 46e65377-9b48-4585-aa72-08781e421abe command.profile.gz --all -o /tmp/ci-firecracker-profiles/27304748140
```

Validation performed:

```bash
bazelisk build //devinfra/buildbuddy_cli:bbapi --config=rbe --remote_download_outputs=toplevel
bazelisk test //devinfra/buildbuddy_cli:bbapi_test --config=rbe
```
