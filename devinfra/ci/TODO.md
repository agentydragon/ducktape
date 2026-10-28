# CI TODOs

## Distinguish ordinary and race test status

`BuildBuddy / Bazel tests (live)` watches only the ordinary test invocation; it
can pass while the later Go race phase is still running or fails. Show the race
phase separately or make the existing status explicit about its scope. Preserve
the overall `bazel-ci / Test & Build` job as the authoritative gate, and handle
skipped race phases, cancellation, and failures without reporting them as passes.

## Consolidate Bazel invocation-role plumbing

`bazel-ci.yml` repeats the test, build, and race roles across ID generation,
linkage, and environment forwarding. Define their role-to-ID mapping once and
derive the other representations from it. Preserve all IDs for diagnostics and
cancellation, while keeping race-configured outputs out of the ordinary test
and build evidence used by release and image publication.

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
  action's `env_overrides`, as `//props/agents:push_images_bin` already does. The
  runner reaches `*.allegedly.works` (observed: it gets HTTP responses from the
  props registry), and as images move to the cluster Forgejo the credential at
  stake stops being a GHCR token.

Decide when images move off GHCR, not before.

## Expand publication test-verdict reuse to suites and patterns

The publication planners now reuse a test verdict only for a literal workspace
test label with one passing attempt in the exact successful push-CI invocation.
They verify the deterministic run/attempt id, checked-out commit, Bazel command
and config flags, RBE image, terminal status, and per-test configuration id.
Cached one-attempt passes remain valid under Bazel's source/config keyed test
cache. Failed, retried, ambiguous, missing, or unreadable evidence keeps the
publication test step enabled.

Suite and pattern selectors still run in their publication jobs. The fast path
requires an exact label's own `testSummary`, so it does not infer suite expansion
from a partial affected-target stream or wildcard match. Extending it requires
reliable test-suite expansion and exact coverage evidence from BEP.

## Scope the undeclared-outputs download narrower than "every test"

`test:rbe --remote_download_regex=...` in <devinfra/bazel/rbe.bazelrc> forces
every test's `test.outputs/` tree onto the runner, though only visual tests need
it there. The cost is small and lands only on tests that wrote undeclared
outputs (<devinfra/docs/bazel_caching.md> § Undeclared test outputs under BwoB),
but the flag is global where the requirement is not.

Uncosted: a second `bazel test` pass over the visual targets carrying the
download flags, with the main `//...` sweep left on plain minimal; or giving
those targets a different execution path entirely.
