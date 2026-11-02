# TODO — Agentplane runner

## Make Environment Runtime COSI names describe their owners and state

Use consistent domain names for desired state, observed status, operation state and controllers.
COSI resources have metadata and an opaque spec; `Status` is a useful project convention, not a
required COSI subresource or a reason to combine resources with different owners. The current
[COSI map](runtime/docs/cosi.md) shows the resource and controller boundaries.

- Rename `runner-lifecycle`, `RunnerDesiredStates`, `RunnerRuntimes`, the `runner` resource ID,
  `runner-host` owner, and the Harness Host `Runner` type around **Harness Host**. In particular,
  `RunnerRuntimes` is the singleton Host lifecycle status and coordination state; a name such as
  `HarnessHostLifecycleStatuses` describes it. Preserve the Host drain barrier and its distinction
  from the outer Runtime close and transition coordinator.
- Rename the other controller-owned `*Runtimes` status resources and Go types consistently:
  `BootstrapRuntimes` → `BootstrapStatuses`, `SessionRuntimes` → `SessionLifecycleStatuses`,
  `CodexAppServerRuntimes` → `CodexAppServerStatuses`, and `EmbeddedExecutionRuntimes` →
  `ExecutionStatuses`. `EnvironmentRuntimeTransitions` and `EnvironmentRuntimeCloses` also hold
  status and should say so. Align type names, constants, readers, diagrams and documentation.
- Rename `environment-runtime-close` and its controller type to reflect that the same controller
  also coordinates adoption and quiesce transitions. Drop `embedded` from the Execution controller
  and resource names where it merely describes how the service is packaged.
- Review resources named `*Inputs` by their actual contents rather than applying one replacement:
  bootstrap carries a request, Session combines demand and observations, Process carries a revision
  wakeup, and Execution carries demand and completion facts. `RunnerInputs` combines admission
  accounting with a copy of Runtime's Execution fence and binding. Consider consuming the applied
  Host intent directly and separating admission facts; first preserve exact boot IDs and generation,
  intent-application ordering, the admission lock and all teardown/fencing barriers.
- Rename `BootstrapOperationResults`, `CodexAppServerOperationResults` and
  `NativeOperationResults`: their resources track operation completion and routing, while the
  in-process operation owns its result. Name the Session resource for Session operations. Preserve
  ownership of one-shot slots, wakeups and consumption; uncertain work must not be replayed.
- Clarify component-specific `Host` types and document or qualify generation, demand, attempt,
  epoch and incarnation identities by their actual scope. Some Session and app-server fields call
  a demand sequence an attempt. Distinguish domain lifecycle phases from COSI metadata phase.

Treat naming changes separately from changes to resource ownership or reconciliation. Resource type
and ID strings can appear in lifecycle blocker diagnostics; update their consumers, tests and docs
in the same change. Keep native handles and durable journal/ACK authorities outside the in-memory
COSI resource snapshots.

## Simplify Environment Runtime reconciliation and coordination

- Make the Codex app-server controller react to the shared Execution lifecycle status instead of
  polling the in-process readiness callback once a second. Retain the dependency deadline and
  verify the live Execution owner as needed; status alone must not admit a Session to a stale or
  unfenced executor.
- Review the in-process Runtime–Harness Host connection, intent, report and wakeup paths. The two
  components now share one process and engine; identify which connection and notification state can
  be removed while preserving synchronous admission revocation, exact binding and applied-intent
  checks, and fail-closed handling of uncertain effects.
- Consider publishing process start, exit, stop and systemd observations as typed facts instead of
  publishing a revision only to wake the controller to read the process registry. Prototype one
  observation first. Live handles and one-shot slots remain process-owned; preserve completion
  ordering, teardown finalizers, fencing and no replay.
- Map the Harness Host drain stages and their dependencies on Session and app-server state. Those
  controllers also wait for the aggregate stage before advancing. Simplify one path at a time with
  race coverage, retaining admission, native cleanup, Execution fencing, sealing and ACK barriers.
- Measure Session reconcile fan-out and contention before optimizing: Host lifecycle updates map to
  every Session, app-server updates map to every Codex Session, and snapshot publication uses a
  manager-wide mutex. If significant, narrow subscriptions or locking without losing the separate
  inspection-settlement signal needed by the drain barrier.

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

## Check whether an interrupted-turn test depends on when Claude flushes its transcript

`native/claude/test_turns.py::test_resume_after_an_interrupted_partial_turn_replays_only_completed_model_history`
interrupts a partial turn, kills the harness right after the result, and sees neither the partial
text nor the thinking replayed on resume. A probe found while fixing the Claude reasoning
reconciliation (#9178), which let the transcript flush before the kill, saw the interrupted turn's
thinking and partial text replayed on resume. Not investigated.

Find out whether the test passes only because of that timing, and whether `claude.py`'s reconcile
and `claude_history.py` must then handle the flushed case.

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
