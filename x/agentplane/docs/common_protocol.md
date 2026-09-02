# Common protocol

The shared seam over Claude Code and Codex is the runner under [`../runner/`](../runner/); its
contract is [`../runner/SPEC.md`](../runner/SPEC.md). This page records what that seam may own,
what stays provider-native, and the vocabulary the layers above it use. The scripted tests under
[`../harness_tests/`](../harness_tests/) and the live probe under [`../capture/`](../capture/)
are the evidence every rule here rests on.

## What the seam owns

- One bidirectional stream per attachment, `Attach`, over session-scoped commands (`Open`,
  `Input`, `Interrupt`, `Shutdown`, `Detach`) and sequenced events.
- Only behavior the scripted tests prove with the real binaries. Its own tests are one
  interaction script per scenario run against both harnesses, so a caller never switches on
  the provider.
- One `Input` verb and no steer verb: both harnesses take an input during a turn into that turn.
- Provider ids and frames, delivered verbatim as `Native` events with the derived events citing
  them, so provider detail is one lookup away rather than collapsed.

The seam does not emulate retry, redispatch an input whose admission is uncertain, or present an
unsupported operation as successful. It is an internal boundary: distinct from the browser-facing
API, and silent on Thread naming, archive presentation, timeline design, and HTTP resource shape.

## Provider differences that stay visible

- Claude and Codex expose different native control and lifecycle frames.
- With `CLAUDE_CODE_MAX_RETRIES` bounded, Claude retries a stream lost before or after visible
  text as a non-streaming request; Codex emits retry notices and eventually fails the turn.
- Codex `turn/steer` requires `expectedTurnId` and joins the running turn; the runner does not
  use it, since a plain `turn/start` during a turn joins it too.
- Steering, queued input, interruption, and resume use provider-native mechanisms and outcomes.
  A related operation on both sides is not evidence that the two are equivalent.

These are constraints on the adapters, not a license to manufacture common semantics the
providers did not demonstrate. A behavior that is unsupported or supported differently is
recorded as such in the adapter, never smoothed over with a generic state machine or retry policy.

## Vocabulary above the seam

The layers above the runner speak in product nouns. They are not fields in native transcripts,
and the runner does not know them:

- **Thread**: a durable, user-visible interaction context, served by one runner session at a
  time.
- **Input**: a message the Agentplane service accepted; it becomes one runner `Input`.
- **Turn**: one provider execution bracket, the runner's `TurnStarted` to `TurnCompleted`.
- **Runner**: the process serving a Thread's session.
- **Timeline event**: a presentation of runner events, owned by whichever app renders it.

## What stays out of the seam

The runner is consumed by, and never defines, the browser-facing API, Thread persistence,
Kubernetes and Agent Sandbox lifecycle, leases and fencing, authorization, credentials,
approvals, subscription adapters, and the conversation UI. None of them reinterprets the raw
evidence; they read the runner's events and its `Native` frames.

## Capture evidence contract

A live probe run preserves native frames in both directions with their complete payloads and
framing boundaries, file order within each transcript, provider-native request, session,
thread, turn, item, and tool ids, model request bodies and streamed response chunks, and enough
process-exit information to diagnose a failed run. Its output stays outside Git as an
investigation artifact; nothing consumes it mechanically, and the behavioral assertions live in
the scripted tests. It carries no routine byte lengths, hashes, parsed-object copies, duplicate
timestamps, sequence registries, or outer copies of provider ids: the ordered payload already
supplies that. Refreshing a pinned harness against it is described in
[the harness tests README](../harness_tests/README.md).
