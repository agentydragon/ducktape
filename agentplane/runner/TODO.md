# TODO — Agentplane runner

## Check whether an interrupted-turn test depends on when Claude flushes its transcript

`harness_tests/claude/test_turns.py::test_resume_after_an_interrupted_partial_turn_replays_only_completed_model_history`
interrupts a partial turn, kills the harness right after the result, and sees neither the partial
text nor the thinking replayed on resume. A probe found while fixing the Claude reasoning
reconciliation (#9178), which let the transcript flush before the kill, saw the interrupted turn's
thinking and partial text replayed on resume. Not investigated.

Find out whether the test passes only because of that timing, and whether `claude.py`'s reconcile
and `claude_history.py` must then handle the flushed case.

## Reconcile conversations that were compacted

`read_history` gives up on a compacted Codex rollout (`compacted`, `thread_rolled_back`) and on a
compacted Claude transcript (`compact_boundary`), so every item of the reconciled turn is reported
unknown. No real compacted history was captured for either harness; the Claude case has only a
synthetic unit test. Capture one of each, work out what a resume loads from it, and report the items
it keeps.

## Codex reconcile edge cases

- A tool call that is not `commandExecution` (a `fileChange`, an MCP call) is always reported
  unknown: the adapter does not match those items to saved function calls.
- A reasoning item's text is compared with the saved record's summary parts joined by newlines. Only
  single-part summaries were captured. If a multi-part summary joined differently, the item would be
  reported revised and the text the operator sees replaced by the saved summary.
- A saved reasoning record without an `id` makes `read_history` raise, so the whole turn is reported
  unknown. Nobody knows whether the routed model ever produces one.

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
