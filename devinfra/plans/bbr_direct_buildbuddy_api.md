# Plan: `bbr` talks to BuildBuddy directly, bypassing `bb remote`

Alternative to <bb_repo_url_commit_override.md> (the recommended, much
smaller primary approach: patch `bb` upstream). This doc is a shelved plan,
not a recommendation to build now — see "Recommendation" at the end.

## Motivation

`bbr` currently shells out to `bb remote`, BuildBuddy's own CLI, which is
pinned as a prebuilt binary (`nix/packages/bb.nix`, `artifacts.bb`). Fully
replacing that dependency would give us:

- Direct control over the repo-URL/commit/patch decision (the thing that
  motivated <bb_repo_url_commit_override.md>) without needing any upstream
  change at all.
- No dependency on `bb`'s binary release cadence or its own bugs (we've
  already hit and worked around two: the `insteadOf`/`github-no-proxy`
  collision, and the stale-`@{upstream}`-base patchset issue tracked via
  buildbuddy#11838).
- One less external tool in the Nix devshell.

Cost: we take on maintaining a client against BuildBuddy's API ourselves,
including tracking any server-side changes, without the version-locked
pairing `bb`'s own release process gives us today.

## Key finding: we already have most of the plumbing

`devinfra/buildbuddy_cli/` (the `bbapi` tool) is **already** a working,
Bazel-built Go client for BuildBuddy's API. It doesn't use raw gRPC — it
calls BuildBuddy's Twirp-JSON API directly (`client.go`):

```
POST https://app.buildbuddy.io/rpc/BuildBuddyService/<Method>
```

with `x-buildbuddy-api-key` auth, `protojson` marshal/unmarshal. This is
the exact same `BuildBuddyService` that `bb remote` talks to over raw gRPC
(`cli/remotebazel/remotebazel.go`'s `bbspb.NewBuildBuddyServiceClient`) —
Twirp services speak both protocols, we just haven't needed the RPCs `bb
remote` uses yet.

Confirmed by reading `buildbuddy-io/buildbuddy@d4e8918` directly (cloned
to a scratchpad, not vendored in-repo):

| What `bb remote` does                | RPC                                                                            | Already in `bbapi`?                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------------ | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Submit the run                       | `BuildBuddyService.Run` (`rnpb.RunRequest`)                                    | No — new command needed                                                                                                                                                                                                                                                                                                                                               |
| Poll for completion + read exit code | `BuildBuddyService.GetExecution` (`execution_stats.Execution`)                 | **Yes** — `cmd_execution.go` already calls this and reads `.GetStage()`/`.GetStatus()`. `Execution.exit_code` (field 8) and `.stage` (`ExecutionStage.Value`, e.g. `COMPLETED`) are exactly what's needed — no need for the raw REAPI `Execution.WaitExecution` streaming call `bb`'s own client uses; polling `GetExecution` is sufficient and reuses existing code. |
| Stream logs                          | `BuildBuddyService.GetEventLogChunk` (chunk-ID pagination, not real streaming) | No — new command needed, but the same simple poll-a-chunk-ID-forward pattern as `GetExecutionDownloads`'s pagination already in `cmd_execution.go`                                                                                                                                                                                                                    |
| Cancel on interrupt                  | `BuildBuddyService.CancelExecutions`                                           | No — small addition                                                                                                                                                                                                                                                                                                                                                   |

All the required proto messages are **already available** as Bazel targets
in `@buildbuddy_protos` (`third_party/buildbuddy/BUILD.protos.bazel`):
`runner_go_proto`, `git_go_proto`, `eventlog_go_proto`,
`execution_stats_go_proto`, `invocation_go_proto` all already exist as
`go_proto_library` rules (someone already fetched the full BuildBuddy
proto set; `bbapi`'s `BUILD.bazel` just doesn't depend on `runner`/`git`/
`eventlog` yet since it never needed them). No new proto compilation work
at all — just add three `deps` lines.

`bbapi` also already has `git.go` using `go-git` (pure Go, no shelling out)
for remote-URL/HEAD detection — the same library could grow patch
generation (tracked diff, binary diff, untracked-file synthetic patches),
replacing `bb`'s own shell-out-to-`git diff` approach
(`generatePatches`, `cli/remotebazel/remotebazel.go:518-612`) with
something more idiomatic for this codebase, though shelling out to the
system `git` (as `bbr.py` already does elsewhere) is also a reasonable,
lower-effort choice.

## Proposed shape

Add a new `bbapi run` command (or similar) that:

1. Resolves repo URL / branch / commit — either from local git state
   (mirroring `determineRemote`/`getBaseBranchAndCommit`'s logic, which we
   partially already have via `check_base_branch_freshness()` in
   `devinfra/bbr.py`) or from explicit flags (this is where we'd get the
   `--repo_url`/pin-a-commit-with-patches flexibility natively, no upstream
   patch needed).
2. Generates a patchset the same way `generatePatches` does.
3. Calls `BuildBuddyService.Run` with a `RunRequest{GitRepo, RepoState,
ExecProperties, Steps: [{Run: "bazel ..."}], ...}`.
4. Polls `GetExecution` (existing pattern) until `stage == COMPLETED`;
   reads `exit_code`/`status`.
5. Polls `GetEventLogChunk` concurrently to tail logs to stdout/stderr.
6. Exits with the remote exit code.

`bbr.py` would then exec `bbapi run <args>` instead of `bb remote <args>`.

## Explicitly out of scope for a first version

- `bb run`'s local-artifact-download path (`downloadOutputs`, bytestream
  CAS fetch, local re-exec) — `bbr` only does `build`/`test`, never `run`
  with local output fetch. Skipping this cuts a large fraction of
  `remotebazel.go` (`downloadFile`, `lookupBazelInvocationOutputs`,
  `bytestreamURIToResourceName`, `downloadOutputs`, the local-exec branch
  of `Run`) that we'd never call.
- Snapshot resume (`--run_from_snapshot`) — not something `bbr` uses today.
- Interactive terminal log redraw (`bb`'s `streamLogs` ANSI cursor
  juggling) — plain sequential log printing (`bb`'s own `printLogs`
  non-interactive path) is sufficient for `bbr`'s use (always
  piped/logged, never an interactive terminal).

## Effort estimate

Meaningfully smaller than an initial "reimplement a BuildBuddy client from
scratch" estimate would suggest, because most of the hard parts
(auth, JSON marshaling, the `Execution` polling pattern, repo/URL
detection) already exist in `bbapi`. Rough shape:

- Add `runner_go_proto`/`git_go_proto`/`eventlog_go_proto` deps: trivial.
- `cmd_run.go` (submit + poll + exit code): similar size/shape to existing
  `cmd_execution.go`/`cmd_invocation.go` (~150-250 lines).
- Patch generation port (tracked/binary diff, untracked files): ~100-150
  lines, whether via shelling out to `git` or `go-git`.
- Log tailing via `GetEventLogChunk`: ~50-100 lines.
- `bbr.py` wiring + removing the `bb` dependency from the Nix devshell:
  small.
- Testing: unit tests for patch generation (can mirror `bb`'s own
  `TestGeneratingPatches`), plus a real end-to-end run against our actual
  BuildBuddy account (`BUILDBUDDY_API_KEY` already available in this
  repo's env) to validate the RPC shapes actually work as read from source
  — this is the one real unknown, since we haven't executed a live `Run`
  call ourselves, only read the reference implementation.

Overall: a few days to a week for a working version covering `bbr`'s
actual `build`/`test` use, not the multi-week "reimplement everything"
estimate from before this investigation — because there's no protocol
reverse-engineering involved (BuildBuddy is open source, the exact
proto/RPC shapes are readable) and most of the client scaffolding already
exists in this repo.

## Ongoing cost (why this is still not free)

Even with the smaller build cost, bypassing `bb` means:

- We stop getting `bb`'s own bug fixes for free (e.g. buildbuddy#11838's
  stale-base-commit fix, which our pinned `bb` version already includes).
  Any future equivalent fix becomes something we have to notice and port
  ourselves.
- If BuildBuddy changes `RunRequest`/`Execution` proto shapes or Twirp
  behavior, we find out by breaking, not by a version bump.
- We'd own log-streaming/retry/backoff edge cases `bb`'s client already
  handles (transient-error retry loop, `DisableRetry`, timeout handling).

## Recommendation

**Don't build this now.** The actual motivating gap — pinning an explicit
repo URL/commit while keeping local patches — is better closed by the
small, backward-compatible upstream patch in
<bb_repo_url_commit_override.md>, which costs a few hours instead of a
few days and doesn't take on any ongoing API-maintenance burden. Revisit
this plan only if a stronger reason to drop the `bb` binary dependency
shows up (e.g., accumulating more `bb`-specific paper cuts, or wanting
tighter control over retry/output handling than `bb remote`'s flags
allow).
