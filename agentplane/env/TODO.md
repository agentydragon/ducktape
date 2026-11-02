# TODO — Agentplane environment

## Prove VM guest boot and console autologin

The current regression checks evaluate the NixOS mount table and rendered VM resources, but do not
build and boot the guest image or prove Runtime readiness. Add VM guest coverage that waits for
Runtime readiness and verifies the serial console shows a login prompt when `console_autologin` is
disabled and a root shell when it is enabled.

## Make Environment Runtime COSI names describe their owners and state

Use consistent domain names for desired state, observed status, operation state and controllers.
COSI resources have metadata and an opaque spec; `Status` is a useful project convention, not a
required COSI subresource or a reason to combine resources with different owners. The current
[COSI map](runtime/docs/cosi.md) shows the resource and controller boundaries.

- Review resources named `*Inputs` by their actual contents rather than applying one replacement:
  bootstrap carries a request, Session combines demand and observations, Process carries a revision
  wakeup, Execution carries demand and completion facts, and the Host input carries admission
  accounting. Preserve the admission lock and all teardown and fencing barriers.

Treat naming changes separately from changes to resource ownership or reconciliation. Resource type
and ID strings can appear in lifecycle blocker diagnostics; update their consumers, tests and docs
in the same change. Keep native handles and durable journal/ACK authorities outside the in-memory
COSI resource snapshots.

## Simplify Environment Runtime reconciliation and coordination

- Review the in-process Runtime–Harness Host connection, intent, report and wakeup paths. The two
  components now share one process and engine; identify which connection and notification state can
  be removed while preserving synchronous admission revocation, exact binding and applied-intent
  checks, and fail-closed handling of uncertain effects.
- Consider publishing process start, exit, stop and systemd observations as typed facts instead of
  publishing a revision only to wake the controller to read the process registry. Prototype one
  observation first. Live handles and one-shot slots remain process-owned; preserve completion
  ordering, teardown finalizers, fencing and no replay.
- Measure Session reconcile fan-out and contention before optimizing: Host lifecycle updates map to
  every Session, app-server updates map to every Codex Session, and snapshot publication uses a
  manager-wide mutex. If significant, narrow subscriptions or locking without losing the separate
  inspection-settlement signal needed by the drain barrier.
- Runtime Close and transition coordination each implement a private worker, completion channel and
  cancellation/join path (`lifecycle_controller.go`, `transition_sequence.go`), while bootstrap,
  Sessions and the app-server use `env/oneoff`. Compare the lifetimes before consolidating: Close
  must preempt transitions, the event loop must stay responsive, and interrupted Execution starts
  must settle their exact fence before another owner can start. `oneoff` publishes COSI completion
  resources and joins a host worker barrier; those semantics are not automatically appropriate for
  the Runtime's coordinator-owned workers.

## Use COSI primitives where they fit the lifecycle

COSI runtime is pinned in `third_party/go.mod`; evaluate its APIs against the actual ownership and
teardown contracts rather than treating every controller as a simple resource transform.

- `env/cosi/resources.go` pre-reads before `safe.WriterModify` to return a `changed` flag.
  COSI's `Modify`/`UpdateWithConflicts` already suppress equal updates and retry update conflicts.
  Audit the extra reads and comparisons, especially the hand-written component wrappers in
  `bootstrap/resources.go`, `harnesshost/session/resources.go`,
  `harnesshost/codexappserver/resources.go` and `supervision/process_resources.go`. Preserve the
  `changed` notifications and aggregate's explicit `QueueReconcile`, the serialized demand
  increment and coherent Session/drain snapshots. A separate pre-read is not an atomic change
  detector; do not replace the locking or report a false change on a conflict retry.
- COSI's generic mappers are not drop-in replacements here. Session operation routing reads the
  operation to ignore its destruction; labels remain present on COSI's destroyed event, so a
  label-only mapper adds a wakeup after consumption. The Process host accepts a namespace at runtime,
  whereas `MapperSameID` derives it from the resource definition, which currently has no default.
- The current lifecycle controllers are not simple `qtransform` candidates. Its transform runs
  inside a retryable `Modify`, potentially before the output is committed or more than once; these
  controllers publish status before effects and notify waiters after writes. Its input finalizer and
  output teardown order also differ from the Process controller's retained `Exited` status and fence
  barrier. Revisit only if a proposed design preserves those contracts; do not move one-shot effects
  into a retryable transform.

## Connect central Codex to environment execution in Pods and VMs

Environment Runtime now manages the standalone Execution Service in Pods and VMs, with typed process
and filesystem operations and explicit ownership/fencing. Connect the central Codex spike
([#12225](https://github.com/agentydragon/ducktape/pull/12225)) to that service so model sampling can
remain outside the Sandbox while execution stays inside it. Exercise both deployment modes with the
central Codex contract, including ownership loss, executor replacement and descendant cleanup.

## Decide whether Agentplane should own a base prompt

Runners preserve the harness's default base prompt and add platform and Session instructions.
Consider an explicit Agentplane base prompt if a production workflow needs one; evaluate it across
the supported harnesses and model routes before replacing their defaults.

## Verify Claude interrupted-turn reconciliation across transcript flush timing

The previously cited `test_resume_after_an_interrupted_partial_turn_replays_only_completed_model_history`
no longer exists. The current native test
[`test_resume_after_an_interrupted_partial_turn_replays_the_partial_turn_and_marker`](../native/claude/test_turns.py)
waits for the interrupt marker to persist and verifies that the partial text is replayed on resume.
It does not establish the no-flush case or the Harness Host's reconciliation result. Compare those
boundaries against `runtime/harnesshost/claude/history.go` and `adapter.go` with real-binary tests;
retain unknown results when the transcript does not prove what survived.

## Reconcile conversations that were compacted

Codex history RPCs report `UNKNOWN` when a compaction item appears in the recovery window;
Claude's transcript reader does the same for `compact_boundary`. The manual-compaction probe in
`native/codex/test_history_projection.py::test_compaction_history_projection_against_resumed_model_input`
shows paginated RPCs retaining pre-compaction item identities and command output after restart while
the next model request uses the generated summary and omits pre-compaction reasoning, function call
and output, and assistant answer. Keep the `UNKNOWN` barrier: those projected items are not proof that
they remain in model context. Automatic compaction, interruption or process loss during compaction,
compaction outside Agentplane's observed recovery window, and external rollback remain unproved.

## Codex reconcile edge cases

- Tools other than `commandExecution` remain unknown until their API history can be compared with
  actual model continuation in real-binary tests.
- Failed or incomplete command executions remain unknown: their API display output does not establish the
  synthetic interruption output sent to the next model request.
- Reasoning recovery compares visible summaries, not the encrypted reasoning payload.

## Claude reconcile edge cases

- A non-streaming retry (a whole message in one assistant frame) followed by an interrupt has no
  runner-level test. The rule counts a frame holding thinking and text as answered.
- A native line that precedes its turn's `TurnStarted` is outside the turn's journal range and is
  not seen. That matters only for a turn whose first frame is a complete assistant frame.
- The interrupt reconcile reads the turn's native lines a second time. Its cost on a large turn is
  unmeasured.
- A `redacted_thinking` block ahead of an unanswered tool call is dropped from the resumed context.
  That was seen with the real binary but no committed test pins it.
- A tool call interrupted before its result is reported unknown on an ordinary interrupt, because
  Claude does not say whether it keeps it.

## Decide what to do with Claude Code's synthetic messages

After an interrupted or killed turn, Claude Code adds messages the operator never typed to the next
model request: `Continue from where you left off.` and `No response requested.` after a kill,
`[Request interrupted by user]` and `No response requested.` after a graceful interrupt. Whether the
thread view shows them is unchecked, and the project has not decided whether it should: surface them
as lifecycle rows, hide them, or treat them as part of the model's context.
