# Journal contention and Runtime stop, 2026-11-15

The Runtime stopped after a Session journal latched `SQLITE_BUSY`. A retained diagnostic command
started scanning every live Session journal twenty seconds before the failure. Its unfinished
read-only `SELECT` could block a DELETE-mode commit; the characterization below reproduces that
mechanism. **The incident-time lock holder and failing SQL statement are still not directly
observed.** Restart clears the journal's latched error even if the original contention has ended.

The separate cleanup stall is fixed by [#16506](https://github.com/agentydragon/ducktape/pull/16506).
Runtime-wide fail-stop and ownership fences remain unchanged.

## Evidence and source boundary

The coordinator supplied the previous-container log excerpts below. Follow-up read-only retrieval
of the retained Runtime logs independently confirmed them. Times are UTC on November 15 in Sandbox
`codex-cleanup-tls`; the retrieved log timestamps use `-08:00`.

| Time               | Reported observation                                                                                                                                                                                                          |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Before 01:19       | Occasional `runner stream lost ingress 5d66022a… no frame from ingress`; no other reported anomaly.                                                                                                                           |
| 01:19:17.547       | First reported reconcile failure for idle Session `521ae64c-28ea-40bb-9e70-665c554b3a55`: `journal failed; reopen it for recovery: database is locked (5) (SQLITE_BUSY)`, `busy=3.94s`. No turn was running in that Session.  |
| Same millisecond   | Every Session's ingress stream reported `context canceled`. Teardown interrupts then failed with `native harness stdin is closed` for Sessions `0057d0f0…`, `1a7b61e1…`, `d28c0a20…`.                                         |
| Through 01:21:43   | The same Session reconcile failed 15 times in total.                                                                                                                                                                          |
| 01:21:47.549       | `environment Runtime cleanup failed: context deadline exceeded`; terminal error was `journal transaction failed: database is locked (5) (SQLITE_BUSY)` joined with `context deadline exceeded`. Runtime exited with status 1. |
| 01:21:48           | Runtime restarted; every Session subsequently reconciled successfully.                                                                                                                                                        |
| Following recovery | `d28c0a20…`'s in-flight turn ended `PROCESS_LOST`, with harness `exit_code=137`. The executor container's last reported OOM was at 00:53:54, outside this window.                                                             |

Read-only Pod inspection independently confirmed the Runtime's exit at `01:21:47`, restart at
`01:21:48`, and executor's previous termination at `00:53:54`. Pod status calls that executor
termination `Error`; the OOM classification comes from the coordinator. Exit status `137` alone
does not establish an OOM in the incident window.

The executable comes from the `environment` image volume,
`agentplane-runner-disk:devel-20261114072809-ccb9cc4`, at
`/run/agentplane/environment/bin/agentplane-environment-runtime`. The container's older
`environment-tools` image tag is not its Runtime source revision. Source inspection and remote
checks used `35a4bf09bb198c14938b5e5d4ddd7e806c7be72e`. Comparison with `ccb9cc4` found identical
journal, Session controller, Codex app-server host, ingress supervisor, Runtime fatal/Close loop
and Close-controller code; the Session manager differs only in a comment.

## Lock ownership: what is ruled out and what remains open

[`journal.Open`](../journal/journal.go) sets `busy_timeout=5000`, `journal_mode=DELETE` and
`synchronous=EXTRA`, limits its `sql.DB` to one open/idle connection, then retains one `sql.Conn`.
Migration runs before retaining that connection. Journal transactions use that connection under
`Journal.mu`, including `WithBatch` publication. These limits are per `sql.DB`, not a global
same-file ownership fence.

The reported text does **not** locate the original failure at `BEGIN IMMEDIATE`. Both failed
`BEGIN IMMEDIATE` and failed `COMMIT` latch `j.failure` and return `StorageError` without naming
the statement. The first reported reconcile already contains `ErrFailed`, which
`checkWritableLocked` returns from the latched error before touching SQLite. Consequently:

- The 15 reconcile failures are not evidence of 15 SQLite lock acquisitions or a lock held for
  150 seconds. Restart clears the latched Go error as well as closing connections.
- A failed `BEGIN IMMEDIATE` could indicate a competing writer. A failed `COMMIT` in DELETE mode
  can instead be a writer waiting for another connection's **SHARED reader lock**. The question
  of who held a RESERVED-or-stronger lock therefore presupposes a statement the logs do not identify.
- `busy=3.94s` is controller log metadata, not a statement trace or proof that the configured
  five-second SQLite busy handler exhausted on that pass.

The [SQLite rollback locking model](https://sqlite.org/lockingv3.html) distinguishes these cases.
The existing `TestJournalCommitWaitsForExternalReader` and `TestFailedCommitReleasesSQLiteLocks`
in [journal tests](../journal/journal_test.go) exercise reader-blocked commits and rollback after
terminal contention. The latter forces immediate contention in its fixture; it does not reproduce
this incident's lock holder or elapsed time.

| Candidate                                                               | Source finding                                                                                                                                                                                                                                                                                                                                     |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Concurrent reconstruction opens the same Session twice                  | Ruled out in the normal manager path: [`Manager.load`](../session/manager.go) holds `loadMu`; `loadLocked` returns the registered Session before opening a journal. `ReserveFresh` and `RetryFresh` use the same lock.                                                                                                                             |
| Rematerialization loses an old Journal                                  | No production removal/replacement of `m.sessions` was found. The sole assignment publishes the first owner. Before publication, unsuccessful loads close the Journal with a defer; after publication, it stays registered for cleanup, including when subsequent publication fails.                                                                |
| `Reopen` creates another SQLite owner                                   | Ruled out: `Manager.Reopen` uses the existing Journal; `Journal.Reopen` deletes the seal and appends an event through `j.write`. Neither opens another database.                                                                                                                                                                                   |
| The pool or migration leaves another connection beside the retained one | The one-connection limit applies during migration too. A checked-out migration connection would prevent `db.Conn` from succeeding, not coexist with the retained one. Open error paths close the database and any retained connection.                                                                                                             |
| A normal failed transaction leaves its writer lock behind               | Write/commit failure paths attempt `ROLLBACK`; failed commits use `failBatchLocked`. The Journal stays failed even after successful rollback. A rollback error would be joined into the error; the supplied terminal text contains none. This does not establish the original failing statement.                                                   |
| An unfinished internal reader spans a write                             | The production `QueryContext` paths in `Since`, `RecoveryTurns`, `UnreconciledProcessLostTurns`, `PendingCommandRecords` and `PendingNativeRequests` close rows before releasing journal ownership. `QueryRowContext` calls scan their rows. No escaping SQL cursor or second internal reader was found.                                           |
| Another ordinary Runtime owns the same state root                       | [`cosistate.Open`](../internal/cosistate/state.go) holds a lifetime exclusive bbolt lock on `runtime-resources.db`, acquired before Session reconstruction. That fences ordinary concurrent Runtimes for the same root, not arbitrary journal openers.                                                                                             |
| Another production component opens this journal                         | Repository search found one production `journal.Open` call site, in the Session manager. Harness recovery uses native APIs, and ingress uses the same Journal object. COSI persistence is bbolt in a different file. SQLite openers in tests and failure-injection fixtures are not production workers.                                            |
| An external reader/writer, path alias, or driver/filesystem fault       | Not established or excluded by these logs. The state volume is shared with the executor; arbitrary workload/diagnostic code can open files without using `journal.Open`. Session path validation prevents traversal, but does not prove absence of symlinks or hard links. Source search cannot inventory incident-time processes or kernel locks. |

These findings provide no supported normal code path for a second leaked Journal. They do not
prove that every opener lived inside the Runtime process. A transient external reader that has
already exited is also consistent with successful recovery after restart.

## Retained diagnostic reader

A retained native tool call at `01:18:57.773 UTC` launched Python in the companion executor to scan
every path matching `/state/sessions/*/journal.sqlite`, in sorted order. For each file its code
used `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`, iterated
`SELECT cursor,payload FROM event_entry`, and decoded the protobuf payload inside that loop. There
was no explicit transaction, paging or cursor close between decoded rows. The scan's target set
includes the affected `521ae64c…` journal. The five earlier retained SQLite diagnostic calls, from
`01:15:07` through `01:16:51`, targeted a different Session's journal.

The relevant shape, with no retained Session content, is:

```python
for path in sorted(glob.glob("/state/sessions/*/journal.sqlite")):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    for cursor, payload in connection.execute("SELECT cursor,payload FROM event_entry"):
        # The incident command decoded each protobuf payload here.
        ...
```

The tool wrapper requested a ten-second yield and printed only `result.output`, discarding the
child process status and any continuation handle. At `01:19:07.943 UTC` it returned an empty
output. That wrapper completion does not establish that the child had exited. No retained child
completion or cursor position identifies which file it was reading at `01:19:17.547 UTC`.
The originating harness turn was interrupted at `01:19:17.727 UTC`.

This establishes a real external SQLite opener immediately before the incident. A slow scan of the
affected journal is a concrete candidate for a reader-blocked `COMMIT`, without a second Runtime or
leaked Journal. It is not a recovered process/lock snapshot, so attributing the exact failed commit
to that reader remains an inference. Current file inspection found distinct journal inodes and no
symlinks or hard-link aliases; it cannot establish incident-time aliases or locks.

`TestUnfinishedReadOnlyScanFailsCommitAndLeavesJournalFailed` in
[journal tests](../journal/journal_test.go) reproduces the mechanism using a separate read-only
connection and an unfinished `SELECT`, with no explicit read transaction or busy-timeout override.
The writer's batch successfully stages an event, establishing that `BEGIN` and the write succeeded;
its `COMMIT` then returns typed `SQLITE_BUSY` while the reader cursor remains open. Closing that
cursor leaves the Journal failed and its published checkpoint unchanged. The test passes on the
existing serving code; it characterizes contention, not a new serving-code regression.

An idle Session is still a possible journal writer: [`Host.readStderr`](../codexappserver/host.go)
records shared Codex app-server diagnostics in every attached Session. Routed stdout frames also
enter Session batches. Neither requires a running turn in that Session. The supplied excerpts do
not identify the originating callback, frame or diagnostic.

## Why the whole Runtime stops

[`SPEC.md`](../SPEC.md#durable-state-and-recovery) requires durable writes before publication;
its shutdown contract says Session recovery or History failure closes admission and starts cleanup
without manufacturing a seal. The [lifecycle contract](../docs/lifecycle.md) also specifies this
Runtime-wide cleanup. Continuing after uncertain durable state is not the implemented contract.

There are several explicit routes from a journal error to that policy:

- [`Host.readStdout`](../codexappserver/host.go) calls a Session attachment's `Frames`, reports
  a failure through `OnError`, and stops the shared app-server process. `readStderr` also reports
  an attachment's journal error. `initializeSessions` wires `OnError` to `Runtime.fail`.
- An individual native reader in [`session/connection.go`](../session/connection.go) reports
  its read/journal failure through the same Runtime error callback.
- A failed Journal follow becomes a local failure in [`ingress.Client`](../ingress/client.go).
  [`Supervisor`](../ingress/supervisor.go) reports it; `startIngress` in
  [`serve.go`](../internal/coordinator/serve.go) fails Runtime and cancels the shared ingress worker,
  canceling every Session's delivery stream.

[`Runtime.failLocked`](../internal/coordinator/runtime.go) revokes admission and signals `fatal`.
`Run` stops the control server and calls `Close`; `Close` publishes a new `Closing` generation for
all Sessions. Failure of one attachment can also stop their shared native app-server before the
global Close drain completes. Closed stdin during teardown is consistent with that path.

A QController returning a reconcile error alone is retried; it does not by itself explain the
fatal signal. The first reported `ErrFailed` reconcile is a symptom of an earlier journal failure,
not necessarily the callback that signaled `fatal`. The terminal unprefixed `StorageError` is
consistent with a native callback; the excerpts do not uniquely identify the callback or which
fatal report won. Ingress cancellation is consistent with the shared ingress failure path.

Thus Runtime-wide admission closure and cleanup are intentional fail-stop behavior, not an
accidental propagation from an otherwise isolated QController. The logs do not establish the exact
signal that killed each harness. Runtime PID-1 container exit also requires outer-runtime fencing;
the in-flight turn's `PROCESS_LOST` is consistent with loss of native execution, not proof of OOM.

## Why cleanup consumes its deadline

The [Close worker](../internal/coordinator/close.go) creates one
`executionFenceGrace` context, defined as **150 seconds** in
[`execution_service.go`](../internal/coordinator/execution_service.go). Native-stop observation,
Execution fencing and subsequent bounded Close stages share that budget. `Run` and its deferred
cleanup both call `Close(context.Background())`; this is the internal fence deadline, not a
cancelled caller deadline. Concurrent Close calls share one worker/result.

The two supplied timestamps differ by `150.002s`. They establish deadline exhaustion, not cleanup
running beyond its deadline by another interval. The deferred Close reports the already failed
shared result, explaining both the cleanup log and joined terminal error.

At the incident revision, the failed Session could prevent native-stop proof as follows
([#16506](https://github.com/agentydragon/ducktape/pull/16506) fixes this path):

1. [`sessionLifecycleController.Reconcile`](../session/controller.go) calls `dispatchControl`
   before entering its non-Running shutdown branch while its phase is not `Halted`.
2. [`dispatchControl`](../session/native.go) consults `PendingCommands` in `Ready` or `Stopping`,
   even without a pending interrupt. The latched journal failure returns `ErrFailed` immediately.
3. Reconciliation returns before scheduling `s.halt` or publishing shutdown progress. Repeating
   the reconcile does not repair that state.
4. The [aggregate controller](../internal/coordinator/session_lifecycle_controller.go) needs every
   retained Session's current-generation `Halted` status before advancing to `Stopping` and proving
   `NativeStopped`. The Close worker waits for that proof or the shared deadline. Close then
   reports failure rather than releasing unproved ownership.

A temporary diagnostic test reproduced steps 1–3 against the inspected source: create a real
Journal, append inside `WithBatch`, return an injected error to latch failure, seed a `Ready`
Session and a `Closing`/`Halting` generation, then invoke its controller. The result was
`ErrFailed`, no halt operation and unchanged `Ready` status. This is a characterization of the
cleanup defect, **not** a reproduction of the original SQLite contention. The temporary test was
removed from the worktree before this documentation PR.

The repeated reconcile failures and 150-second interval fit this mechanism. No incident-time
Session/aggregate/Runtime-Close status snapshot or goroutine stack was supplied, so the exact
stalled barrier in this incident remains inferred, not directly observed. Other outstanding native
work or an Execution fence can also consume this budget. The closed-stdin warnings alone do not
prove which barrier remained incomplete.

## Checks and remaining evidence

- `bbr test //agentplane/env/runtime/journal:journal_test --test_output=errors`
  passed with the unfinished read-only scan characterization
  ([invocation](https://app.buildbuddy.io/invocation/a86f04d1-8bf1-4503-aa6c-b2455b668196)).
  The test leaves the production busy timeout unchanged and demonstrates the `COMMIT` failure after
  successful staging. No live journal was opened by this follow-up investigation.
- `bbr test //agentplane/env/runtime/journal:journal_test //agentplane/env/runtime/session:session_test --test_output=errors`
  passed both baseline targets from cache on `787b5834`
  ([invocation](https://app.buildbuddy.io/invocation/ddae5073-95ab-4c33-b64d-8c81929430c3)).
- `bbr test //agentplane/env/runtime/session:session_test_binary --test_filter=TestDiagnosePoisonedJournalPreventsHalt --test_output=all`
  passed the temporary characterization ([invocation](https://app.buildbuddy.io/invocation/8410357c-9b4e-4b71-81e1-3c46ca706264)).
  Its first build attempt failed because the diagnostic incorrectly used generic `Publish` for
  aggregate status; it was corrected to `WriteStatus` before this run.
- `bbr test //agentplane/env/runtime/journal:journal_test //agentplane/env/runtime/ingress:ingress_test //agentplane/env/runtime/internal/coordinator:coordinator_test --test_output=errors`
  passed all three targets from cache ([invocation](https://app.buildbuddy.io/invocation/abaffafa-52eb-40bf-ae40-e2838d1cd681)).
  These cover journal contention/rollback, History failure and Runtime cleanup ownership; they do
  not establish the incident's lock owner. No system suite injects the incident diagnostic reader.

To settle the original cause, the missing evidence is the first failing SQL boundary
(`BEGIN`, operation or `COMMIT`), its caller/stack and journal inode, plus contemporaneous open
descriptors and SQLite byte-range locks for that inode across processes. Any backup/diagnostic
reader active at `01:19:17` matters even if it exited immediately afterward. Runtime-Close and
aggregate blocker snapshots would identify the cleanup stage. Current post-restart locks cannot
recover those facts. Diagnostic instrumentation would need a separate change; none is introduced
here. Increasing `busy_timeout` or blindly retrying cannot identify or repair the cause.

## Second stop: duplicate Codex turn boundary

The same Sandbox stopped again at `15:19:24 UTC` on November 15, still running runner-disk
`ccb9cc4`. This time the journal latched `SQLITE_CONSTRAINT_PRIMARYKEY` (`1555`) while inserting
`turn_boundary.turn_id`. Previous-container logs and retained Session evidence identify a delayed
live notification trying to start a turn already recorded from its `turn/start` reply.

The coordinator reported a History rollout at `15:06:54`, an ingress EOF/CONNECT `502` followed by
recovery, and executor OOM at `15:08:13`. Read-only Pod inspection confirms the executor was
`OOMKilled`, exit `137`, at `15:08:13`; Runtime's previous logs confirm the embedded Execution
backend-loss fence. Neither event establishes loss of the separately supervised native Codex
app-server. In particular, this Session's journal does **not** record a `PROCESS_LOST` completion
at `15:08`: its recorded active turn ends on Runtime restart at `15:19:26`.

For Session `551f2a33-886b-4fd8-bdf1-9857058435c7`, the journal contains:

| UTC time       | Cursor  | Evidence                                                                                               |
| -------------- | ------- | ------------------------------------------------------------------------------------------------------ |
| `14:09:08.863` | `3100`  | Reply-derived start of turn `01a1df8a-5895-7be0-aa76-48fa83293690` (A).                                |
| `14:55:11.439` | `13259` | `turn/start` reply names `01a1dfb2-fe20-7ba0-9af5-47958696c8e5` (B).                                   |
| `14:55:12.853` | `13260` | Reply-derived `TurnStarted` for B replaces A as the active turn.                                       |
| `15:02:10.675` | `14555` | Another `turn/start` reply names `01a1dfb9-4326-7b52-9de0-d5ab6995cdef` (C).                           |
| `15:02:11.245` | `14556` | Reply-derived `TurnStarted` for C replaces B as the active turn.                                       |
| `15:16:52.408` | `17334` | Live `turn/completed` notification for A; no neutral completion follows because A is no longer active. |
| `15:16:53.197` | `17336` | Live `turn/started` notification for B, on the original native thread.                                 |
| `15:16:54.224` | —       | First logged reconcile error reports the latched duplicate-boundary failure.                           |
| `15:19:24.227` | —       | Cleanup exhausts its fence; Runtime exits with the constraint error and deadline error.                |
| `15:19:26.008` | `17338` | Startup recovery records C as `PROCESS_LOST`.                                                          |
| `15:19:44.095` | `17341` | First retained `thread/resume` request after the failure.                                              |

SQL inspection used private byte copies, never a SQLite connection to the live journal. Both
copies passed `PRAGMA quick_check`; every historical event in the first copy matched the second,
which had additional later entries. The second copy's source size/mtime stayed unchanged during
the copy, and no rollback sidecar was observed. These checks corroborate the retained prefix;
ordinary byte copying is not a general consistent-backup procedure.

At `ccb9cc4`, [`codexAdapter.OnFrame`](../internal/harnesses/codex/adapter.go) suppresses
`turn/started` only when its ID equals the Session's **current** active turn. At cursor `17336`,
that ID is C, so B passes the guard. `Session.Emit` commits the raw Native batch before appending
the derived turn transition. The raw notification survives, then the boundary insert collides
with B's existing row at cursor `13260`; the failed transaction never publishes a second start.
Journal failure reaches the unchanged Runtime-wide fail-stop and the old cleanup stall described
above. Repeated reconciles report the latched error rather than retrying the insert.

This establishes duplicate projection of an already observed turn, without requiring a new native
ID collision. The triggering notification precedes recovery's first `thread/resume`, and
`codexAdapter.Recover` replays replies, not notifications. The journal also retains the newer
reply-derived active turn before the executor OOM. Native ID reuse after an app-server restart
and Runtime recovery replay are therefore unnecessary explanations for this failure. The reason
Codex delivered B's notification so long after its reply is not established by these records.
The relevant adapter and Session event code differs between `ccb9cc4` and the investigated devel
base `e22b6d44` only in wording.

The candidate correction makes both reply and notification handling consult the existing indexed
turn boundary. It retains raw Native evidence, emits no second start for a known ID and leaves
the current turn unchanged. It adds no durable fields, migration, event rewriting, retries or
fail-stop exception. The lookup does not load historical turn payloads.

`TestCodexDelayedTurnStartNotificationDoesNotFailJournal` in
[Session tests](../session/native_test.go) uses the real SQLite journal and a fresh Codex adapter.
It covers a known uncompleted turn followed by another active turn, and a known `PROCESS_LOST`
turn followed by another active turn. Both retain the delayed frame, preserve the current turn
and admit a genuinely new start. With only the test added to `e22b6d44`,
`bbr test //agentplane/env/runtime/session:session_test --test_output=errors` fails both cases with
the incident's exact `UNIQUE constraint failed: turn_boundary.turn_id (1555)` error
([invocation](https://app.buildbuddy.io/invocation/e4545c43-6974-4150-9d13-077b7bc0fe5c)).
The Session, Codex adapter and journal targets pass with the correction
([invocation](https://app.buildbuddy.io/invocation/ebc97da4-6f89-4b27-bd66-9df01bd305f4)).
This reproduces the duplicate-start failure, not the executor OOM or the notification's delay.
The real-harness `//agentplane/env/e2e:{test_codex_shared_process,test_turns}` suites and
`//agentplane/env/runtime/internal/coordinator:coordinator_test` also pass
([invocation](https://app.buildbuddy.io/invocation/b26df193-5486-449e-946f-9d5e00a4d6c2)).
They cover ordinary turns, shared app-server process loss/recovery and Runtime ownership/cleanup;
they do not inject the incident's delayed notification.

### Retained History projection

`agentplane sessions read-turns` with count `100` returns a source `DataLoss` error, `retained
History contains an incomplete turn projection`, at the boundary preceding cursor `13260`.
The client projection rejects starting B before A has completed. That inconsistency predates
the OOM and the duplicate notification. The journal still has open boundaries for A and B;
only C receives a neutral `PROCESS_LOST` completion during restart. Preventing the duplicate
insert does not repair those records or establish correct future overlapping-turn projection.

### Executor memory observations

The first requested `ps` snapshot, at `15:51 UTC`, contains no Bazel server or language server.
The largest process is `/bin/codex exec-server` at `61544 KiB` RSS; the PID-1 execution companion
uses `19708 KiB`. This is an executor snapshot after restart, not evidence of the OOM-time
allocation owner. The subsequent cgroup sample has approximately `947 MiB` of file-backed memory
and `208 MiB` of anonymous memory; process RSS alone cannot explain the whole cgroup charge.
No historical process or cgroup allocation snapshot was recovered for `15:08:13`.

After the remote tests and local pre-commit finished, the second snapshot at `16:02 UTC` still
has only the exec-server (`38480 KiB` RSS), companion (`19624 KiB`) and the snapshot command.
There are no leftover build or language-server processes. The cgroup holds approximately
`1714 MiB`: `1627 MiB` file-backed, `18 MiB` anonymous and `66 MiB` kernel memory. Most file-backed
memory is unmapped; `file_dirty` is `24 KiB`, `unevictable` is zero and the cgroup reports no OOM
or limit-hit events in this executor incarnation. This sample's increase is dominated by file
cache, not a growing resident process. It does not prove what exhausted memory before restart.
All builds/tests used `bbr`; no local Bazel server was started.

## Proposals

If Session-local storage failures should instead leave other Sessions running, that is a separate
policy decision: it must cover shared app-server readers, History supervision and admission/fence
ownership. This note proposes considering that isolation; it does not adopt or implement it.
Both observed Session journal failures took the shared Runtime down; the second instance
strengthens the availability case without changing the durability and ownership decision.

The affected Session's History needs a separate data decision. Options are to preserve it as
evidence with the explicit read failure, or authorize a scoped reconstruction from retained native
events after defining the correct turn ordering. If the Session is disposable, deletion requires
the operator's explicit scope and acceptance of the loss. No option is executed here. Independently,
reply-before-notification ordering needs a follow-up contract/test for distinguishing accepted
future work from the currently executing turn; suppressing duplicate starts alone cannot supply
the missing completions.
