# Native harness evidence

The Claude and Codex drivers are grounded in live captures against LiteLLM and scripted tests that
run the pinned native harnesses against a loopback model endpoint. The probe under
[`../capture/`](../capture/) preserves ordered native frames and model bodies outside Git; the
tests under [`../harness_tests/`](../harness_tests/) carry the committed behavioral contract.

## Observed provider behavior

- Claude uses newline-delimited stream/control JSON. With `CLAUDE_CODE_MAX_RETRIES` bounded, the
  pinned version retries a stream lost before or after visible text as a non-streaming request.
- Codex app-server uses newline-delimited JSON-RPC-shaped messages. The pinned version emits retry
  notices and eventually reports a failed turn after repeated upstream losses.
- A Claude active-turn second input is native input behavior; it is not steering unless native
  evidence supports that interpretation.
- Codex exposes `turn/steer` and `turn/interrupt`; `turn/steer` requires `expectedTurnId` and joins
  the running turn.
- Codex switches to code mode for model ids in its built-in catalog. Scripted tests use an unknown
  model id to exercise the classic function-call shape.
- Claude's session-title model call is suppressed by `--name`.
- Both harnesses run with thinking or reasoning enabled; tests assert that the thinking signature
  or encrypted reasoning is echoed back upstream.
- Idle resume uses Claude `--resume` or Codex `thread/resume`, never transcript replay or prompt
  redispatch.

These are observations from the currently pinned harnesses. The runner must not manufacture retry,
steering, acknowledgement, or successful-completion semantics that the native process did not emit.
An unrecognized native kind remains a named `Unknown*` value so captures can reveal protocol drift
without silently discarding it.

## Evidence contract

The scripted scenarios cover launch and handshake, streaming and terminal output, tool lifecycles
and workspace effects, active-turn input, interruption, upstream retry and exhaustion, follow-up
after transport failure, and idle native resume. Assertions synchronize on protocol checkpoints,
not sleeps, and check scenario-relevant markers rather than volatile packet equality.

A live probe records native stdin, stdout, stderr, model request bodies, streamed response chunks,
and controlled loss markers. It never records HTTP headers, cookies, credentials, OAuth state, or
private user data. File order is the ordering authority; do not add redundant hashes, lengths,
timestamps, parsed copies, or manifest inventories.

When a harness pin changes or a new behavior needs coverage:

1. run the live probe against a synthetic workspace and the real model endpoint;
2. compare the native and model exchanges with the scripted expectations;
3. update the driver and scripted scenario only for behavior Agentplane intends to support; and
4. update the harness pin.

Classify provider-specific results as **Proven**, **Supported differently**, **Unsupported**, or
**Environment-blocked** instead of normalizing them into a false common success.
