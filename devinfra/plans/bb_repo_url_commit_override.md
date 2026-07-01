# Proposal: `--repo_url` / `--apply_local_patches` flags for `bb remote`

Primary approach for decoupling "which repo/commit the remote runner clones"
from local git state, as an alternative to the current `github-no-proxy`
remote workaround. See <bbr_direct_buildbuddy_api.md> for the (larger,
not recommended for now) alternative of bypassing `bb` entirely.

## Problem

`bb remote` (`cli/remotebazel/remotebazel.go`) infers everything about what
to clone on the runner from local git state:

- **Repo URL**: `determineRemote()` runs `git remote -v` and picks a fetch
  remote (prompting/caching via `.git/config` if there are several). There
  is no flag to just say "use this URL."
- **Base commit**: `--run_from_commit`/`--run_from_branch` exist, but
  `Config()` treats either one as "give me a clean checkout" — setting
  either **disables patch generation entirely** (`generatePatches` is only
  called `if *runFromBranch == "" && *runFromCommit == ""`). There's no way
  to pin an exact base commit and still mirror local working-tree changes
  on top of it.

We already worked around the URL half of this in `agentydragon/ducktape`
(`devinfra/claude/reconcile_bbr_remote.sh`): point a dedicated
`github-no-proxy` remote at a real, direct GitHub URL and tell `bb` to use
it via the `buildbuddy.remote-bazel-remote-name` git-config key that
`determineRemote()` already reads. That's sufficient for our current use
case and needs no upstream change.

What it can't do: pin an **explicit commit** while still applying local
patches. Today that's an all-or-nothing choice between "auto-detect
everything, including patches" and "explicit ref, no patches." A workflow
that wants "run my dirty working tree, but diffed against this specific
SHA rather than whatever `bb` auto-detects" has no knob for it.

## Proposed patch

`devinfra/plans/bb_repo_url_commit_override.patch` (generated against
`buildbuddy-io/buildbuddy@d4e8918`, `cli/remotebazel/remotebazel.go` +
`remotebazel_test.go`). Adds two new flags to the existing `RemoteFlagset`:

- **`--repo_url`**: use this URL as the fetch URL instead of
  `determineRemote()`. Requires `--run_from_commit` (there's no local
  remote to resolve a branch/commit against once URL auto-detection is
  skipped) — enforced with an explicit error, not a silent fallback. Falls
  back to a new `buildbuddy.remote-bazel-repo-url` `.git/config` key when
  the flag itself is unset, mirroring how `determineRemote()` already
  caches the picked remote name under `buildbuddy.remote-bazel-remote-name`
  — so session-setup tooling can write this once instead of every
  `bb`/`bbr` invocation needing the flag. Flag takes precedence over the
  config key when both are set.
- **`--apply_local_patches`**: when combined with `--run_from_branch` or
  `--run_from_commit`, still calls `generatePatches(commit)` instead of
  skipping it. Default `false`, so existing `--run_from_commit`/
  `--run_from_branch` users keep today's clean-checkout behavior — this is
  purely additive.

`Config()` changes to:

1. Use `*repoURL` as `fetchURL` when set, skipping `determineRemote()`
   (and `determineDefaultBranch()`, which only makes sense relative to a
   resolved git remote).
2. Pass an empty `remoteName` into `getBaseBranchAndCommit()` in that case
   — safe, because that function's very first branch already short-circuits
   on `*runFromBranch != "" || *runFromCommit != ""` and returns before
   `remoteName` is ever used.
3. Generate patches whenever `(*runFromBranch == "" && *runFromCommit == "")
|| *applyLocalPatches` — i.e. the existing auto-detect path, plus the
   new opt-in.

Design goal: **zero behavior change for existing flags.** Both new flags
are opt-in and additive; nobody using `--run_from_commit` today for a clean
checkout is affected.

### Why not just change `--run_from_commit`'s existing behavior?

Considered and rejected: `--run_from_commit`'s docstring is explicit ("If
unset, the remote workspace will mirror your local workspace"), meaning
existing users likely rely on it for exactly the clean-checkout guarantee.
Silently starting to apply local patches on top would be a breaking change
for them. A new opt-in flag is strictly safer to propose upstream.

## Status

- Patch is hand-written against the real source (cloned at commit
  `d4e8918`), `gofmt`-clean, and includes new test cases
  (`TestGitConfig_RepoURLOverride`, covering the rejection case, the flag
  with/without `--apply_local_patches`, the `.git/config` fallback, and
  flag-over-config precedence) following the existing
  `TestGitConfig_FetchURL`/`TestGitConfig_BranchAndSha` harness style
  (`testgit.MakeTempRepo`/`MakeTempRepoClone`/`CommitFiles`).
- **Not** compiled or run — `buildbuddy-io/buildbuddy` is a large Bazel
  monorepo; `go build`/`go test` don't work standalone without pulling in
  Bazel-generated proto packages (`go build ./cli/remotebazel/...` fails
  immediately on missing `proto/*` packages that only exist as
  `go_proto_library` outputs). Actually running the tests needs their
  Bazel setup, not attempted here.
- Not yet submitted upstream.

## To use this

1. **Before upstream accepts it**: nothing needed for our current use
   case — the `github-no-proxy` remote-name workaround already covers
   repo-URL selection, and we don't currently have a workflow that needs
   an explicit-commit-plus-patches run. This patch is for a future need,
   not blocking anything today.
2. **To submit upstream**: fork `buildbuddy-io/buildbuddy`, apply
   `bb_repo_url_commit_override.patch`, get it building/passing under
   their Bazel setup, open a PR referencing this use case (decoupling
   local git remote/branch state from the runner's checkout target for
   sandboxed/proxied CI environments).
3. **To consume locally before upstream merges** (if a real need shows
   up): `nix/packages/bb.nix` currently installs a prebuilt release binary
   (`artifacts.bb`), not a from-source build — there's no patch hook today.
   Using a patched `bb` locally would mean switching that derivation to
   build from source (e.g. `buildGoModule` or vendoring their Bazel build
   via Nix) with this patch applied, which is nontrivial and not attempted
   here. Simpler stopgap: keep using `--run_from_commit`'s existing
   clean-checkout mode (no patches) for any interim explicit-commit need.

## Sequencing

This PR is the patch/design proposal only — deliberately kept separate
from the bootstrap fixes already merged via #2696, since they're
independent and this one has no working code to run yet (there's nothing
in the Nix devshell that consumes it). Once a patched `bb` build exists in
the devshell (step 3 above), a follow-up PR switches
`devinfra/claude/reconcile_bbr_remote.sh` to write
`git config buildbuddy.remote-bazel-repo-url <url>` instead of (or
alongside) today's `github-no-proxy` remote-name trick, and `bbr.py` can
start using `--repo_url`/`--apply_local_patches` where useful. Not done
here — no working `bb` build exists yet for bootstrap to target.
