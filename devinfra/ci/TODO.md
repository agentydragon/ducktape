# CI TODOs

## Consider pushing images from the BuildBuddy runner

Today the push job downloads the OCI layout to the GitHub Actions runner and
`crane push`es from there, so registry credentials never enter the runner VM.
That download is the last consumer of `bb remote build`'s post-run output
download — the mechanism behind every publish bug in
`devinfra/debug/bb_remote_peers_exhausted.md`.

Two ways out, neither urgent:

- Fetch the layout's files from CAS by digest on the GitHub runner. The stream
  reports every file with its `pathPrefix`, so the layout can be rebuilt without
  Bazel. Credentials stay where they are.
- Push from inside the runner VM, forwarding credentials through the bb-remote
  action's `env_overrides`, as `//props/agents:push_images` already does. The
  runner reaches `*.allegedly.works` (observed: it gets HTTP responses from the
  props registry), and as images move to the cluster Forgejo the credential at
  stake stops being a GHCR token.

Decide when images move off GHCR, not before.

## Gate publishing on the verdicts bazel-ci already recorded

Every publish job re-runs its item's tests as a gate. The same build event stream
carries a `testSummary` per target — on devel's `//...` sweep, 979 of them,
correctly reporting the two that timed out. A publish could consult that instead
of re-running, which also gives per-target granularity rather than per-pattern.

`devinfra/ci/bes.py` already parses them (`Invocation.test_status`); nothing
consumes it yet.

## Visual publishing races the invocation it reads

A superseded run's publish fires the instant its cancelled CI run completes,
while Bazel is often still streaming that commit's results — cancelling the
workflow does not cancel the invocation. The publish takes what BuildBuddy holds
at that moment, frequently nothing, and never comes back. Measured: `29154c806`'s
publish ran 14:29–14:31 and found no manifests; its sweep finished at 14:32:27
with all 34.

Accepted for now because the damage is bounded to staleness —
<devinfra/pr_visuals/README.md> states why a partial read can leave a baseline
pointer old but never move it somewhere wrong. On a busy devel day baselines run
a few commits behind and PR comments carry more `baseline_fallback` warnings; a
quiet period heals it.

Two ways out:

- Gate on the invocation's terminal state. `SearchInvocation` reports
  `invocationStatus`; the publisher could poll to `COMPLETE_INVOCATION_STATUS`
  before reading artifacts, cheap now that the download itself is ~17s. Unchecked:
  what BuildBuddy reports for a genuinely in-flight sweep, never sampled live.
- Export from the BuildBuddy runner via `bb remote -script`, where the artifacts
  are local files under `bazel-testlogs/`. That deletes the race rather than
  narrowing it — the export runs inside the invocation instead of reacting to a
  workflow cancelled while the invocation carried on — and drops the CAS round
  trip with it. It does not require BuildBuddy Workflows. It is the same
  credential trade as § Consider pushing images from the BuildBuddy runner:
  `pr-visuals-publish.yml` deliberately keeps the S3 credential off any runner
  that PR-controlled code touches, and the same escape hatch applies.

## Scope the undeclared-outputs download narrower than "every test"

`test:rbe --remote_download_regex=...` in <devinfra/bazel/rbe.bazelrc> forces
every test's `test.outputs/` tree onto the runner, though only visual tests need
it there. The cost is small and lands only on tests that wrote undeclared
outputs (<devinfra/docs/bazel_caching.md> § Undeclared test outputs under BwoB),
but the flag is global where the requirement is not.

Uncosted: a second `bazel test` pass over the visual targets carrying the
download flags, with the main `//...` sweep left on plain minimal; or giving
those targets a different execution path entirely.
