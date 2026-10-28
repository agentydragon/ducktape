# TODO — Agentplane runner

## Run executor mode as the execution location for central Codex in Pods and VMs

Make `agentplane-runner executor` usable as the main Agentplane process in either a Pod or a VM,
so the central Codex spike ([#12225](https://github.com/agentydragon/ducktape/pull/12225)) can place
execution in either environment without running the full Runner, Session lifecycle or journals
there. Today this mode requires PID 1 in a private container.
Select supervision explicitly for each environment: preserve the Pod container-lifetime fence and
use VM service/cgroup supervision for the VM execution domain. Exercise both deployment modes with
the central Codex execution contract, including ownership loss, executor replacement and descendant
cleanup; do not infer the environment from the process ID.

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
