# BuildBuddy integration

BuildBuddy supplies hosted Bazel runners, remote execution, and build artifacts.
This directory owns the tools and integration code specific to that service.

- [`bbr.py`](bbr.py) wraps `bb remote` with session metadata, a Git-base check,
  and inner invocation-ID tracking. The repo-root [`.bbrc`](../../.bbrc) supplies
  native runner defaults for both `bbr` and GitHub Actions.
- [`bbapi/`](bbapi/) contains our Go API CLI, distinct from upstream `bb`.
  Its [command reference](skill/buildbuddy_api/SKILL.md) covers invocation state,
  build-event exports, artifact/blob downloads, and cancellation. Read RPCs use
  the generated gRPC client; mutations, suggestions, and download routes use HTTP.
- [`skill/buildbuddy_api/`](skill/buildbuddy_api/) packages the API skill.
- [`ci/`](ci/) contains invocation readers, artifact downloads, cancellation,
  live test status, runner probes, and CI linkage helpers. General release and
  publication orchestration remains in [`devinfra/ci`](../ci/).
- [`images/runner/`](images/runner/) defines the outer `bb remote` runner and
  its build/publish helper. [`images/rbe/`](images/rbe/) defines the environment
  for individual build and test actions. Each keeps experiments in its `x/`.
- [`docs/`](docs/) explains [setup](docs/integration.md),
  [remote execution](docs/remote.md), and [RBE workflows](docs/rbe_workflows.md).
  [`debug/`](debug/) holds investigations; [`x/`](x/) holds experimental tools.

Image pins and their shared updater remain in [`devinfra`](../image_pins.json).
Workspace execution policy remains in [`devinfra/bazel/rbe.bazelrc`](../bazel/rbe.bazelrc).
GitHub Actions and Nix package definitions reference these implementations from
`.github/` and `nix/` respectively.

From the repo root, run the existing tests with:

```bash
bbr test //devinfra/buildbuddy/... --config=ci
```

## Census GitHub Actions BuildBuddy invocations

[`ci/invocation_census.py`](ci/invocation_census.py) derives deterministic
invocation IDs from an existing Actions snapshot using the same helper as CI.
It requires an exact Actions job name and an explicit comma-separated role
selection. Event, workflow-path, and run-created time filters are optional.
The snapshot format is a JSON object with `runs` and `jobs` arrays, such as the
`snapshot.json` emitted by the CI report collector.

Run from the repo root with a local `bbapi` available in the Nix devshell:

```bash
OUT="${TMPDIR:-/tmp}/ci-latency-invocation-census"
mkdir -p "$OUT"
bb run //devinfra/buildbuddy/ci:invocation_census_bin -- \
  --snapshot "$OUT/snapshot.json" \
  --job-name 'bazel-ci / Test & Build' \
  --roles test,build \
  --events push,pull_request \
  --since 2026-10-24T00:00:00Z \
  --until 2026-10-30T00:00:00Z \
  --max-workers 4 \
  --timeout-seconds 30 \
  --output "$OUT/invocation-census.json"
```

The live query calls the existing `bbapi invocation status` command through
the repository wrapper. Concurrency is bounded (default 4, maximum 32), and
each request has a bounded timeout (default 30 seconds, maximum 300). The JSON
output contains allowlisted metadata, never BuildBuddy logs or raw error text.
`actions.actions_run_head_sha` records the Actions run head separately from
`buildbuddy_commit_shas`, which lists the commit SHA BuildBuddy reports for the
invocation. A pull-request Actions SHA may be a synthetic merge SHA.

States retain what the status API reports: `complete`, `disconnected`,
`in_progress`, `unknown`, `mixed`, `missing` (an empty invocation response),
and `query_error`. BuildBuddy's `success` field stays in metadata; the census
does not infer flakes. A missing role's derived ID is reported as `missing`
when BuildBuddy returns no invocation, not as a test failure. Per-ID query
errors are included in output and make the command exit nonzero; refreshed
output is assembled from the current query without reusing an earlier partial
status.

For deterministic offline replay, pass `--offline-responses-dir DIR` with
`<invocation-id>.json` status response files. Replay output is marked
`query_mode: "offline_replay"`; a stored `COMPLETE_INVOCATION_STATUS` is
historical status evidence and does not prove a fresh physical test execution.
In replay mode, `observed_at` is the time of the replay; the original API
observation time is unavailable unless it was saved separately.
BuildBuddy can reuse cached actions, so use execution records when the question
is whether tests physically ran.

The published executable names (`bbapi`, `bbr`) and Nix output names are unchanged.
