# History fold rejects a retained attachment

## Evidence

On 2026-11-15, `history-service-77b4f9c69d-68dv2`, running
`devel-20261115042724-08f5e49`, logged `Thread fold stalled; will retry` for two Sessions.
The first errors were at `07:37:41.930258772Z` for
`91397b91-a732-41e3-bfa8-ae79a8977efa` and `07:37:41.976917381Z` for
`f247d7fd-1ffd-483f-af22-e9d6108d83ef`. Each appeared 11,015 times through
`09:18:06Z`, with a median interval of about `0.418 s`. The errors were strict ProtoJSON
failures: `unknown field "executionPrincipal"`, at columns `710` and `704` respectively.
The other replica, `history-service-77b4f9c69d-sxkbm`, ran the same image and logged none.
The pods were created at `04:31:44Z` and `04:32:26Z`; the first error was not their startup.

Approved read-only PostgreSQL transactions inspected the two Sessions in `sandbox_service`.
Action receipts: `ca7021ae-a1e4-4f23-8935-1c1475bbae37` and
`5aec9481-589b-4e4f-88cb-55bb9eec8065`. No incident rows or cluster resources were changed.

| Session                                | Raw tail | Fold checkpoint / saved attachment cursor | First folded Event, UTC | Last folded Event, UTC  |
| -------------------------------------- | -------: | ----------------------------------------: | ----------------------- | ----------------------- |
| `f247d7fd-1ffd-483f-af22-e9d6108d83ef` |     2228 |                                      1685 | Nov 12, 09:32:22.695671 | Nov 12, 09:40:50.319250 |
| `91397b91-a732-41e3-bfa8-ae79a8977efa` |      940 |                                       682 | Nov 12, 09:32:22.789702 | Nov 12, 09:40:53.573500 |

Both `thread_history_summary.attached` values contain `executionPrincipal`, naming account
`agentplane-staging/sampler-storage-smoke-20261112`, UID
`307ff43f-8765-4cf1-95e6-7593e00cee12`. Both History writer bindings name ServiceAccount
`agentplane-staging/central-sampler`, UID `87cc988d-6957-4312-a54b-a93e1d2f72cf`, and Sandbox
`agentplane-staging/sampler-storage-smoke-20261112`, UID
`1ee22ec8-f0d4-414c-bb7f-f8bcec5c6df3`.

The offending stored values are attachment snapshots in the JSON projection, not identified
raw Events. The ordinary Event rows cover cursors `1..2228` and `1..940`; none contains the
literal UTF-8 string `executionPrincipal`. Those payloads are protobuf binary, so a string search
alone cannot rule out historical binary fields. Wire-field inspection of the current
`session_history.feed_state` attachments found cursors `2228` and `940`, with no reserved
attachment field `9`. The current feed snapshots and saved JSON projection are different states.

## Writer and schema change

Before commit `a87c044744896456e40638689c0069f36da7a8f7` (PR #15903, merged
2026-11-13 at `03:30:10Z`), `central_sampler/store_sessions.go`'s `Describe` copied the creation
request's `ExecutionPrincipal` into `Attached`. The sampler's History announcement supplied that
attachment; History's `internal/projection/lifecycle.go` serialized it with `protojson.Marshal`
into the summary. The producer was the sampler; the JSON projection writer was History.

That commit reserved `Attached.execution_principal` (field `9`) as part of the Identity-only
execution contract. The retained JSON still carries its old name. The queried tables do not record
the producing image or snapshot-write wall clock. The folded Event timestamps establish the
Nov 12 prefix, not the exact time at which the snapshot was written.

## Trigger at 07:37

`session_history` changes to the committed prefix or feed state mark a Session pending through
`mark_session_fold_pending`. Explicit settlement/refold work can also queue a Session. The
pending versions observed were `610883` and `610913`. The newer feed cursors are consistent with
subsequent sampler activity making the old projection eligible again.

The exact operation at `07:37` is unresolved. Pending rows have neither enqueue timestamps nor
caller audit; `track_commit_timestamp` is `off`, so their transaction IDs cannot recover a commit
time. The retained logs establish when the fold first tried, not who queued it. No inspected
record proves a replay, manual requeue, settlement, or particular sampler request at that time.
A contemporaneous command/audit record is needed to distinguish them.

## Hot retry and failure surface

`projectLifecycle` strictly decodes the _saved_ attachment before considering the current feed
snapshot. It returns the bare ProtoJSON error, so `Projector.ProjectBatch` does not classify it
as unfoldable and does not call `recordFailure`. Each page transaction rolls back, preserving
checkpoints `1685` and `682`. The saved `view_state/current` rows still report
`THREAD_FEED_STATUS_ACTIVE`, with those cursors as `lastVerifiedCursor`.

`FoldRunner.advance` logs every per-Session error and returns no progress. The pending row stays,
and each claim renews the same owner's 30-second lease. `Run` waits for either the nominal
`fold_interval_s` (default `2 s`) or a global History-change signal. Unrelated commits therefore
wake deterministic failures sooner than the interval; there is no per-Session backoff or parking.
The foreign live lease keeps the second replica off these Sessions, explaining the asymmetric logs.

Healthy Sessions still fold under the runner's bounded concurrency. The failures consume database
work, worker slots and log capacity, but neither stop the runner nor fail its health service.
For the affected Sessions, Thread rows remain at their last verified checkpoint and Electric
continues replicating those unchanged rows. Raw History ingress and reads are separate paths.

Summary reads are an additional blast radius: `internal/reads/summaries.go` strictly decodes the
same saved attachment. `GetSessionSummary` fails for an affected Session, and
`BatchGetSessionSummaries` fails the whole request if it includes either one. Healthy Session
metadata in such a batch is therefore unavailable too. Parking the fold does not repair those
summary reads or make these Sessions current.

## Handling permanent failures

Saved attachment/end decoding failures are classified as inconsistent projections. An unfoldable
failure is marked permanent for scheduling only after the fenced failure transaction succeeds.
The existing generic stalled diagnostic and `FAILED` operational state surface the problem at
the last verified checkpoint; detailed decode errors remain in backend logs. Database/lease errors
while retaining the diagnostic remain retryable. A failure without a materialized checkpoint is
logged without inventing a view or Event source.

The owner parks a permanent failure's pending version in memory, retaining and renewing its lease.
Unrelated commits and periodic cycles do not rerun that version. A new pending version or new
owner retries once; restart also clears the local park, allowing a new image to re-evaluate it.
This bounds retries for unchanged state under one owner. It does not resolve incompatible data,
prevent repeated attempts if the Session itself keeps changing, or persist parking across rolls.

`TestPermanentAttachmentFailureParksOnlyItsPendingVersion` reproduces the saved legacy field in
a disposable PostgreSQL fixture. Before the serving change, this invocation failed because the
feed remained `ACTIVE` instead of surfacing its permanent failure:

```bash
bbr test //agentplane/history_service/internal/projection:integration_test \
  --test_filter=TestPermanentAttachmentFailureParksOnlyItsPendingVersion --test_output=errors
```

Pre-change invocation: `f1e02513-77d2-401d-bffe-c7463ddeea2f`. The test also checks continued healthy
folding, retained raw Events and pending rows, no repeated diagnostic for an unchanged version,
one retry for a new version/owner, and retry when recording the diagnostic first fails. No system
suite currently injects legacy projection JSON; the PostgreSQL integration test covers that case.

## Proposals

The two disposable smoke Sessions still need an operator decision. Options are to retain them
with failed projections, separately authorize rebuilding their disposable projection from
validated authoritative binary History, or separately authorize their targeted retirement.
Before choosing a rebuild, verify that all authoritative inputs decode and that the desired
projection is reconstructible. No rebuild, rewrite, backfill, tolerant reader, retirement or
delete is implemented here. The strict reader and raw retained History remain unchanged.
