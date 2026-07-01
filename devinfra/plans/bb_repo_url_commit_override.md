# Proposal: `--repo_url` flag for `bb remote`

Primary approach for decoupling "which repo/commit the remote runner clones"
from local git state, as an alternative to the current `github-no-proxy`
remote workaround. See <bbr_direct_buildbuddy_api.md> for the (larger,
not recommended for now) alternative of bypassing `bb` entirely.

## Problem

`bb remote` (`cli/remotebazel/remotebazel.go`) infers everything about what
to clone on the runner from local git state:

**Repo URL**: `determineRemote()` runs `git remote -v` and picks a fetch
remote (prompting/caching via `.git/config` if there are several). There
is no flag to just say "use this URL."

We already worked around this in `agentydragon/ducktape`
(`devinfra/claude/reconcile_bbr_remote.sh`): point a dedicated
`github-no-proxy` remote at a real, direct GitHub URL and tell `bb` to use
it via the `buildbuddy.remote-bazel-remote-name` git-config key that
`determineRemote()` already reads. That works, but it means keeping a
second git remote around just to give `bb` a URL it can reach — the
existing `origin` remote's tracking refs, branch/commit auto-detection,
and patch generation are all otherwise perfectly fine as-is.

## Proposed patch

`devinfra/plans/bb_repo_url_commit_override.patch` (generated against
`buildbuddy-io/buildbuddy@d4e8918`, `cli/remotebazel/remotebazel.go` +
`remotebazel_test.go`). Adds one new, opt-in flag to the existing
`RemoteFlagset`:

**`--repo_url`**: use this URL when fetching on the remote runner, instead
of the local remote's own URL. This is a **pure URL substitution** —
`determineRemote()` still runs exactly as before to pick which local
remote's tracking refs to use (so multi-remote prompting/caching via
`buildbuddy.remote-bazel-remote-name` is untouched), and
`determineDefaultBranch()`/`getBaseBranchAndCommit()`/`generatePatches()`
still auto-detect branch/commit/patches against that remote's own
`refs/remotes/<name>/...` state exactly as today. Only the final
`RepoConfig.URL` string sent to the runner is swapped. This means no
`--run_from_commit` is needed just to use a different URL: `bb test` and
`bb build` keep working with zero extra flags once the override is set,
the same as any normal invocation — you just get `origin`'s ordinary
auto-detected branch/commit/patches, fetched from a different URL. Falls
back to a new `buildbuddy.remote-bazel-repo-url` `.git/config` key when
the flag itself is unset, mirroring how `determineRemote()` already
caches the picked remote name under `buildbuddy.remote-bazel-remote-name`
— so session-setup tooling can write this once instead of every
`bb`/`bbr` invocation needing the flag. Flag takes precedence over the
config key when both are set.

`Config()` changes to compute `fetchURL` via a new `repoFetchURL(remote)`
helper (flag, then `.git/config`, then `remote.url` as the default) in
place of the current unconditional `fetchURL := remote.url`. Everything
else in `Config()` — `determineRemote`, `determineDefaultBranch`,
`getBaseBranchAndCommit`, the patch-generation condition — is untouched.

Design goal: **zero behavior change for existing flags, and no new flags
required for the common case.** `--repo_url` is opt-in and additive;
nobody using today's flags is affected, and using it (or its
`.git/config` fallback) doesn't require learning or passing any other new
flag.

## Status

- Patch is hand-written against the real source (cloned at commit
  `d4e8918`), `gofmt`-clean, and includes new test cases
  (`TestGitConfig_RepoURLOverride`, covering: the flag substituting the
  URL while branch/commit/patches still auto-detect normally, the
  `.git/config` fallback, flag-over-config precedence, and the
  no-override-set default) following the existing
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

1. **Before upstream accepts it**: nothing needed right now — the
   `github-no-proxy` remote-name workaround already covers repo-URL
   selection today. But this patch is a strictly nicer replacement for it
   whenever it lands: one `git config buildbuddy.remote-bazel-repo-url
<url>` on the existing `origin` remote, instead of maintaining a whole
   second remote just to give `bb` a reachable URL.
2. **To submit upstream**: fork `buildbuddy-io/buildbuddy`, apply
   `bb_repo_url_commit_override.patch`, get it building/passing under
   their Bazel setup, open a PR referencing this use case (decoupling the
   URL the runner fetches from from the URL used for local git
   operations, for sandboxed/proxied CI environments).
3. **To consume locally before upstream merges** (if a real need shows
   up): `nix/packages/bb.nix` currently installs a prebuilt release binary
   (`artifacts.bb`), not a from-source build — there's no patch hook today.
   Using a patched `bb` locally would mean switching that derivation to
   build from source (e.g. `buildGoModule` or vendoring their Bazel build
   via Nix) with this patch applied, which is nontrivial and not attempted
   here.

## Sequencing

This PR is the patch/design proposal only — deliberately kept separate
from the bootstrap fixes already merged via #2696, since they're
independent and this one has no working code to run yet (there's nothing
in the Nix devshell that consumes it). Once a patched `bb` build exists in
the devshell (step 3 above), a follow-up PR switches
`devinfra/claude/reconcile_bbr_remote.sh` to write
`git config buildbuddy.remote-bazel-repo-url <url>` on the existing
`origin` remote instead of maintaining the separate `github-no-proxy`
remote, and `bbr.py`/CI can drop the second-remote setup entirely. Not
done here — no working `bb` build exists yet for bootstrap to target.
