# Runtime control acceptance

Status: deferred design. This page records why the concrete runtime reasoning-effort change was
withdrawn and what a future protocol decision has to resolve. It is not a new runner contract.
The current `SwitchModel` contract remains in the [runner specification](../runner/SPEC.md#model-changes).

The corresponding task-DAG item is [`CONTROL_STATE`](../plans/task_dag.md#control_state--dynamic-runtime-control-acceptance).
The concrete [REASONING_EFFORT_RUNTIME proposal in #6456](https://github.com/agentydragon/ducktape/pull/6456)
is no longer an implementation plan.

## Why the implementation recipe was difficult

The first design mirrored the existing model-switch path: add a runner command and success/rejection
events, reject it while `active_turn_id` is set, and make both adapters fit that gate. That is a
reasonable common denominator, but it makes the runner decide a harness question and prevents a
useful class of changes during an active agent loop. It also makes a superficially identical
success mean different things in the two adapters:

- Claude can receive a native `control_request` with subtype `set_model`. Agentplane's Claude
  adapter sends that request and waits for the native control response.
- Codex app-server has no corresponding runtime model-mutation request in the pinned surface.
  Its model is supplied on `thread/start` and can be overridden on `turn/start`; the pinned native
  surface also lists a per-turn `effort` field. The current adapter persists the new runner default
  and lets the next `turn/start` carry the model, while it supplies effort through the
  `model_reasoning_effort` thread/config setting.
  A second `turn/start` while a turn is active joins that turn, so a model field on an in-loop
  input is not automatically a model switch for that input.

The pinned native surface and coverage are summarized in the [protocol roster](../native/docs/protocol_roster.md).
The runner's existing cross-harness test proves idle model selection reaches the next upstream
request for both providers; it does not prove that an active-loop model mutation has equivalent
semantics. The same distinction matters more for reasoning effort: Claude's `--effort` is a launch
setting in the current adapter, while Codex's native per-turn effort field and the
`model_reasoning_effort` thread/config setting are separate surfaces. Neither is evidence that one
common runtime mutation exists.

## The semantic question

The useful protocol concept is not a persistent harness capability. It is an operation-specific,
time-local control state: for example, "the harness would accept a model change sent now" or "it
would not accept one now." The meaning should be _now_, with no promise about a later point. A state
transition could therefore say that acceptance has changed, but it cannot replace the response to
the command: state and command can race, and the command response is the authoritative result.

Before adding commands for model or reasoning-effort changes, the protocol needs a decision on:

- admission versus effect: does accepting a request mean the harness admitted it, applied it to the
  current model request, or scheduled it for a named subsequent request or turn?
- active-loop changes: if a model change is admitted while an agent loop is active, can it affect a
  later model request in that same loop, or only the next turn? How is that effective model recorded
  separately from the session's standing default?
- provider-specific state: how do Claude's native control-request boundary and Codex's per-turn
  fields produce compatible observations without inventing equivalence the harnesses do not have?
- reconnect and races: what state is included in `Attached`/`SessionSummary`, what transitions are
  replayed, and what does a client do when its state view is stale?
- effort proof: what native Claude and Codex exchanges demonstrate a reasoning-effort change, and
  what does each harness actually promise about when that change takes effect?

A negotiated control-state surface may be the right direction, but it should be designed around
these questions rather than added as a mirror of `SwitchModel`. No protocol, adapter, bridge, UI, or
reasoning-effort behavior change is included in this documentation PR.

See also the [common protocol](common_protocol.md), which records the rule that provider behaviors
supported differently must remain visible, and the [harness evidence contract](harness_evidence.md)
for the proof required before promoting a provider-specific behavior into the seam.
