# Patched `bb` CLI pin

<!-- CLEANUP(added 2026-08-27): Delete this directory and
  .github/workflows/bb-patched.yml, and repoint the `bb` entry in
  nix/artifact-pins.json at the stock buildbuddy-io/bazel release, once a bb
  CLI release carries buildbuddy#13067. -->

The stock `bb` CLI cannot ship a **binary file deletion** in its `bb remote`
patchset: its `file --mime`-based classifier cannot classify a deleted path,
the deletion lands in the plain-text diff as a content-less stub, and the
runner's `git apply` fails with `cannot apply binary patch … without full
index line` — breaking every run while such a deletion is uncommitted-ahead
of the diff base (<../../devinfra/docs/bb_remote_internals.md> § Gotchas).
Upstream fix: [buildbuddy#13067](https://github.com/buildbuddy-io/buildbuddy/pull/13067)
(approved, unreleased). Until a release carries it, the `bb` pin ships our own
build with that fix cherry-picked.

## Contents

- `pr13067.patch` — `git format-patch` of upstream commit
  `b24085c522e4539cb43a4421643ab2f2af5ce642` (the #13067 head;
  EOF-normalized by pre-commit). Applies cleanly onto `cli-v5.0.387`
  (`e85d89e0bf52…`), the version the pin tracks.
- `build_bb.sh` — applies the patch onto a `cli-v5.0.387` checkout and runs
  the same Bazel invocation as the linux-amd64 leg of upstream's
  `release-cli.yaml` (`--stamp -c opt --strip=always --config=static`,
  musl-static). The binary reports `bb version` → `5.0.387-pr13067`.

`.github/workflows/bb-patched.yml` runs the build on changes to this
directory and publishes the binary as a content-addressed GitHub release
(`bb-patched-<sha12>`, same scheme as the `release.yml` pins), which
`nix/artifact-pins.json` then points at.

Deviation from the other self-published pins: this is not a row in
`devinfra/ci/artifact_targets.json` because the input is upstream BuildBuddy
source, not a ducktape Bazel target — BuildBuddy's module graph only builds
with their repo as the Bazel root (root-only `single_version_override`s), so
it gets its own workflow instead of the release matrix.
