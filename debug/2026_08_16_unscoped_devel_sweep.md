# Does devel need a scheduled unscoped sweep? (2026-08-16)

Prompted by `//haku/console:test_mcp_server` breaking on `devel` on 2026-08-16 with
`alembic.script.revision.MultipleHeads: … 0043, 0043`, and by the hypothesis that the break stayed
invisible for hours because per-PR `bazel-ci` is bazel-diff scoped. The scoping is real; the
invisibility was not. Recording the measurements so the proposal is not re-derived from the same
hypothesis.

## `devel` already gets an unscoped `//...` run per push

`devinfra/ci/bazel_ci.sh` selects the affected set only when `PR_HEAD_SHA` is non-empty. `ci.yml`
triggers on `push: [main, devel]`, and a push event forwards no `PR_HEAD_SHA`, so a merge to `devel`
takes the `TARGETS="//..."` branch. BuildBuddy agrees — every devel-push invocation records
`Command: test //...`:

```text
$ bbapi invocation dfcd65fa-9614-4b79-891a-989808d831a6
Command:     test //...
Commit:      94137ac3
Role:        CI
```

The same script turns target tracking on only for `push`, so `bbapi target history` for this repo is
exactly the devel/main sweep timeline and nothing else.

## What actually happened on 2026-08-16

`bbapi target history --label //haku/console:test_mcp_server --since 24h`:

```text
PASSED  2026-08-16 02:16  94137ac3
FAILED  2026-08-16 01:56  a623d391
FAILED  2026-08-16 01:45  32d5f658
FAILED  2026-08-16 01:42  6874c198
FAILED  2026-08-16 01:41  a2e0d1ce
PASSED  2026-08-16 01:20  e6c22676
```

The break entered at `a2e0d1ce` (#4119, merged 01:37:56) and left at `0b64777e` (#4124, merged
02:01:29) — about 24 minutes and seven commits, over which the unscoped sweep ran and failed four
times. Scanning every `devel` commit's `haku/console/migrations/versions/` for two files declaring
the same `revision` finds the duplicate `0043` on exactly those seven commits and nowhere else in
the last two days. So the gate detected it, promptly, without a scheduled run.

Two `devel` commits in that burst (`36a2b2a05b`, `342dc92834`) have no sweep at all: `ci.yml`'s
`concurrency: cancel-in-progress` is keyed on the ref, so merges a minute apart cancel each other.
That costs attribution, not detection — the next merge's sweep still covers the tip.

## Why a scheduled sweep adds little

A re-run of `//...` on an unchanged `devel` commit is served from the remote cache: those
invocations complete in 20s–1m against 12m cold. Cheap, but it returns the same cached verdict, so
it carries no information the push-triggered sweep did not already produce. Making it informative
needs `--nocache_test_results`, which buys flake and environment-drift detection — a different
problem from the one above, and priced accordingly.

The sweep would matter if `devel` went quiet for a long time after a red commit. Merge rate here is
tens per day, so that window is small.

## The gap that is real

Four consecutive red `//...` sweeps on `devel` produced no notification. Nothing in
`.github/workflows/` reacts to a failed push-event run; the only scheduled workflows are
`prune-releases.yml` and `sync-pins.yml`. Recovery happened because a human was already looking.
Whatever reports a red `devel` is worth more than another producer of the same red — but it is
reporting, not coverage, and it wants the operator's own preference for where an alert lands.

The cheaper guard is `//haku/console:test_migration_graph`, which fails at PR time on the file
shape rather than at deploy time on the graph.
