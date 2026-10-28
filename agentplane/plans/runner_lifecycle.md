# Runner lifecycle simplification plan

Status: design in review, 2026-10-27; no runtime implementation selected by this PR.
The operator requested this design and plan after selecting an outer lifecycle machine,
separate setup/execution state and reconciliation loops. The
[design](runner_lifecycle_design.md) owns the proposed state model and invariants; the
[task DAG](task_dag.md#runner_lifecycle_design--runner-lifecycle-state-and-ownership)
owns scheduling and dependencies.

## Review and implementation slices

Review the design's ownership, Close/Quiesce interaction, setup/execution split and native
reconciliation boundary. Preserve the existing public protocol and journal semantics. Merge of
this documentation does not authorize an unbounded runner rewrite.

Try COSI before selecting controller infrastructure; the state/ownership design and
runtime-independent shutdown changes can proceed while that comparison is open. The trial is
planned, not implemented, and does not select a production dependency.

The three implementation slices below share the reviewed design as a prerequisite. Recommended
first runtime slice: shutdown. This is a review/iteration preference, not a technical dependency:
Session preparation and reconciliation scheduling can be developed independently against the
reviewed transition boundaries. Rebase overlapping edits rather than invent sequencing holds.

### `RUNNER_COSI_TRIAL` — evaluate COSI on a bounded shutdown path

Prototype one runner quiesce path with [COSI Runtime](https://github.com/cosi-project/runtime),
using in-memory resources for intent/progress and existing journal/store facts for durable truth.
Keep process handles and native effects behind owned operations. Compare the prototype with
extending the current loops against the same lifecycle contract; do not start a general migration.

Deliver a scoped spike with a runnable Bazel check, its exact results and a short adopt/reject
comparison. Demonstrate with deterministic operation barriers that:

- quiesce racing admission preserves durable ownership/deduplication and closes new ordinary work;
- teardown stops shared processes once, seals after final writes and stays unquiesced until the
  exact seal is acknowledged;
- asynchronous completions/native replies keep progressing, and a late completion cannot revive
  stopped execution or cause a native effect to be repeated by controller retry.

Record the resource/controller ownership graph, finalizer behavior, restart reconstruction,
dependency/logger integration cost and custom bookkeeping retained or removed. Conclude whether
to adopt COSI, use it narrowly, or retain the existing loops. Put the supported recommendation in
the design for review before framework-specific implementation. The trial does not substitute for
the later implementation matrix's Claude/Codex checks and needs no manual staging UI run.

### `RUNNER_SHUTDOWN_OWNER` — outer lifecycle and shutdown transitions

Replace the `seal bool` argument with explicit shutdown intent. Give the Runner one owner for
admission closure and drain coordination, and give each Session an owned stop operation with
explicit reason. Move all corresponding flag mutations in halt, startup-failure cleanup, process
exit and incarnation reopening through those boundaries. Preserve intentional-exit and turn
attribution separately from the durable seal and acknowledgement.

Use existing Session preparation/work interfaces while converting this concern; do not require
the other slices' new representation. Join concurrent Close/Quiesce cleanup without duplicate
stops, lost seal intent or reopening same-incarnation admission after drain failure. Preserve the
state owner descriptor until processes, workers and journals have completed cleanup.

Exit: shutdown no longer depends on freely assigned stop/seal/teardown flags; admission races,
retryable drain failure, setup completion, shared-process stop, seal acknowledgement and durable
reopen pass their focused automated checks.

### `RUNNER_SESSION_LIFECYCLE` — owned Session preparation and execution

Replace readiness/activation flags with the execution phase and one preparation operation. Move
resume metadata into the attempt, and route prepare/ready/exit completion through the Session
owner. Validate operation and process incarnation on completion. Keep setup on its existing
one-shot state, including a running setup with dormant execution before the first input.

Preserve lazy native startup, concurrent sends sharing one attempt, admission without waiting for
native I/O, and public diagnostic interpretation. Keep Claude process ownership and shared Codex
conversation preparation distinct. Use the current shutdown interface until the shutdown slice
lands; reconcile the boundaries on rebase without leaving two readiness authorities.

Exit: startup, loss, preparation cancellation and subsequent recovery retain command/context
evidence for both harnesses; a late completion cannot ready a stopping/successor attachment;
completed setup does not rerun.

### `RUNNER_RECONCILIATION_OWNER` — queued and in-flight inspection

Make native inspection a queued/in-flight operation owned by the Session work loop. Preserve the
committed-terminal-event barrier and exact-turn native-write gate. Trigger progress from recorded
changes; remove independently assigned `reconciling` state once dispatch reads the operation owner.
Keep stdout/reply routing independent of blocking inspection.

Coordinate halt with inspection that needs a live attachment; classify uncertain continuation
using existing adapter evidence/unknown reports. Sealing cannot race outstanding journal writes.
Expose busy/status facts through the same owned snapshot without changing autosuspend predicates,
heartbeat cadence or public command ordering.

Exit: an empty pending queue while inspection is in flight still blocks dispatch; concurrent
kicks/interrupts, native history RPC replies, process loss and shutdown preserve ordering and
uncertainty without replaying native effects.

## Acceptance and validation

Each slice adds or updates deterministic cases for its changed boundary. Tests prove behavior,
not the spelling of enum values or the number of flags. Prefer controlled barriers over sleeps.

| Boundary                    | Required evidence                                                                                                                                                                                               |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Admission and quiesce       | A racing send is durably owned or rejected for retry/wake; identical IDs retain their original admission; changed idle tokens reject conditional quiesce                                                        |
| Setup and bootstrap         | Creation can run setup with dormant execution; orderly close/quiesce waits for running scripts; restart does not rerun successful/interrupted setup                                                             |
| Startup                     | Concurrent inputs share preparation; startup failure settles affected commands; cancellation/late completion cannot install a stale attachment                                                                  |
| Interruption and inspection | Stale target rejects before admission; an admitted interrupt cannot affect a successor; terminal observation precedes inspection; queued/in-flight inspection blocks dispatch while stdout delivers RPC replies |
| Loss and uncertainty        | Crash/restart uses native evidence, retains unknown outcomes and never blindly repeats an uncertain native effect; resumed follow-up proves context retention for Claude and Codex                              |
| Shared processes            | Codex exit affects every attached Session; shutdown stops the app-server once after Sessions halt; Claude remains independently owned                                                                           |
| Sealing and adoption        | Final writes precede the seal; exact sealed cursor acknowledgement gates `QUIESCED`; failed drain stays closed/retryable; same-incarnation restart stays closed; new-incarnation reopen is durable              |
| Status and autosuspend      | Internal setup/execution separation preserves public summaries and busy predicates; readiness alone does not count as busy; no extra heartbeat/polling                                                          |

Primary iteration target: `bbr test //agentplane/runner:runner_test` (already race-enabled).
Use `//agentplane/sandbox_controller:sandbox_controller_test` for quiesce/status boundaries and
the existing runner `test_restart`, `test_turns`, `test_initialize`, `test_attach` and
`test_codex_shared_process` targets for affected native behavior. Inspect their existing harness
parameterization and retain both Claude and Codex coverage. Run the affected Pod/VM system tests
when changes reach transport, durable stop/seal or guest integration; keep these tests on RBE.
The system-test topology and deterministic controller coverage are documented in
[Sandbox lifecycle coverage](../system_tests/k3s/README.md#sandbox-lifecycle-coverage).

This documentation PR runs formatting/link checks and PR CI; runtime checks above are future
implementation obligations, not claims of validation already performed. No manual staging UI
drive is added or repeated. Do not change production lifecycle timing merely to exercise a plan.

## Completion and rollback

Each implementation PR states its changed ownership boundary and exact checks. Graduate landed
contracts into component documentation and remove finished DAG/plan entries; delete these proposal
files when their remaining content has graduated. Do not leave a permanent migration shim or two
mutable state representations after a concern is converted.

Keep wire/storage shapes unchanged so a runtime slice can be reverted normally. Any discovered
need for a persisted schema or protocol change is a separate reviewed change with data-preserving
rollback; never edit a landed migration or delete Session/native state to make the refactor pass.
