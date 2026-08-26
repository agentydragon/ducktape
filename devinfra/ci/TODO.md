# CI TODOs

## Plan image pushes from the build event stream

`plan_releases.py` no longer builds anything: it reads what bazel-ci already
built from BuildBuddy's build event stream and derives each release's identity
from the digests reported there. `plan_image_pushes.py` still runs its own
`bb remote build` of 42 `.digest` targets, and bazel-ci already built all 42 —
measured on devel's `//...` sweep, every one of them is in the stream.

Images need one step more than releases: a release's identity _is_ its output's
digest, whereas an image's identity is the _content_ of its `<name>.json.sha256`
file. Those are ~70 bytes each and `devinfra/ci/bes.py` already exposes
`fetch_blob`, so the planner fetches 42 small blobs instead of allocating a
runner.

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

## Releases bazel-ci cannot prove

Three rows always take the slow path, each for a principled reason. They are not
bugs, but a reader hitting the fail-open warnings should know why:

- `aw-importer` — `@ducktape_activitywatch//importer:aw_importer_bin` is in an
  external repo, which `//...` does not reach.
- `gterm-theme` — `//gnome/gterm_theme:*` is `tags = ["manual"]` (needs
  libgirepository, libdbus), so wildcards skip it.
- `debundle` — builds under `-c opt`, a different configuration from bazel-ci's.
