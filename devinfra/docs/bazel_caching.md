# Bazel Caching in CI

[BuildBuddy](../buildbuddy/README.md) caches action results across Bazel runs.
For GitHub-hosted workflows that invoke Bazel directly, the
[setup action](../../.github/actions/setup-bazel/action.yml) also caches the Bazelisk
download and the content-addressed Bazel repository downloads. Its module-file hash
and shared restore prefix allow reuse across workflows and dependency updates.
The repository cache path assumes the GitHub-hosted `runner` username.
BuildBuddy-hosted `bb remote` runs use a separate runner filesystem.

Caching the whole local Bazel state exceeded the GitHub cache quota. The expanded
LLVM toolchain accounted for most of that state. The compressed archives fit under the quota in the
original measurements ([repository cache change](https://github.com/agentydragon/ducktape/pull/363)).
The unified cache action saves only on an exact-key miss. Unconditional saves once
created duplicate entries that broke restores with HTTP 400
([cache action change](https://github.com/agentydragon/ducktape/pull/747)).

## Undeclared test outputs under BwoB

`--remote_download_minimal` (Build without the Bytes) singles out test runner
actions. `RemoteOutputChecker.addTargetUnderTest` adds a test's outputs to the
download set only when `outputsMode != MINIMAL`, so under minimal the
`test.outputs/` tree stays remote-only.

That is invisible until a cache hit. BEP's per-test file list comes from
`TestRunnerAction.getTestOutputsMapping`, which stats each path on the local
filesystem, and Bazel does not populate the output filesystem on an action-cache
hit — an upstream limitation with a standing TODO in that method. A test served
from the local action cache therefore reports `test.log` and nothing else, while
the same test served from the remote cache reports its full `test.outputs/`,
because that path materializes the outputs. `devinfra/pr_visuals` consumes
exactly those artifacts, so a visual test that hit the local cache published
nothing and the run looked like it had no visual changes.

`test:rbe --remote_download_regex=bazel-out/.*/testlogs/.*/test.outputs(/.*)?`
puts the tree back in the download set. That fixes the cache-hit path too, not
just execution: `RemoteOutputChecker.shouldTrustMetadata` refuses to trust a
remote-only output that should have been downloaded, so the action cache cannot
hit while `test.outputs/` is remote-only. The cost lands only on tests that
wrote undeclared outputs — an empty tree has no children to distrust — and it is
a remote-cache round trip, not a re-execution.

The pattern spans the whole path because `RegexPatternOption` matches the entire
string, and its `.` is unescaped because a bazelrc consumes the backslash —
`--announce_rc` shows what Bazel actually received.
`shouldDownloadOutput` checks the child path and the tree root, so a pattern
matching either is enough.

### Rejected: --remote_download_toplevel

The blunt form of the same fix, and it does work — but `toplevel` downloads
every top-level target's important artifacts, which on `bazel test //...` means
every OCI image tarball lands on the runner. The regex asks for the few hundred
kilobytes of PNGs that BEP actually needs.
