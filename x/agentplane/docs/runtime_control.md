# Runtime control

`ChangeModel` is an implemented durable runner command. The cross-layer command
lifecycle and UI contract are authoritative in
[Thread, runner, and harness layering](thread_layering.md). An app acceptance or
runner admission is not a model change; only the causal `ModelChanged` Event says the
change took effect.

`CommandAdmitted` says only that the runner stored the command. There is no public
timing selector or generic active-turn conditional: the runner waits for
harness-native evidence.

## Harness evidence

- Claude receives a native `control_request` with subtype `set_model`. Its adapter
  emits `ModelChanged` only after the successful native control response. Claude
  accepts this control while a turn is active; the pinned harness test proves the
  next model request uses the selection.
- Codex app-server has no equivalent mutation request. Its adapter retains the
  requested model until the next native `turn/start` response proves that selection.
  A later pending request supersedes an earlier one, which ends as `CommandNoop`; a
  native refusal ends both the selected model command and input command as
  `CommandFailed`.

The app must allow subsequent input to reach the runner while a model change is
pending: Codex needs that input's `turn/start` to apply the selection. Admission and
scheduling are separate; no app gate may wait for each command's terminal effect.

The pinned native surface and mock-LLM tests are summarized in the
[protocol roster](../native/docs/protocol_roster.md).

## Open capability question

A future capability snapshot can say, for one operation at one instant, whether the
runner could promptly apply it or would retain it while busy. This is advisory and races with
a command. Before adding it, demonstrate for each harness:

- which native exchange establishes admission, application, or future-turn selection;
- how an active turn's effective model remains separately auditable;
- what replay/reconnect state it needs; and
- whether an admitted command can recover to effect, failure, or no-op after relevant
  crash windows.

Reasoning effort needs its own evidence. Claude's launch configuration and Codex's
per-turn/model configuration surfaces are not proof of one common live mutation, so
it is not a runtime command.
