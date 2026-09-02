# Common protocol: post-capture design notes

Status: **the shared seam is in place as the runner under [`../runner/`](../runner/), with its
contract in [`../runner/SPEC.md`](../runner/SPEC.md); what remains here is the evidence contract and
the product nouns the service seam will introduce**.

The native drivers under [`../native/`](../native/), the scripted tests under
[`../harness_tests/`](../harness_tests/), and the live probe under [`../capture/`](../capture/)
supply the provider evidence. That evidence is the input to protocol design; it must not be
replaced by more bookkeeping or by a speculative compatibility framework.

## Evidence already available

For both native harnesses, the scripted tests cover:

- launch and handshake;
- a baseline streamed turn;
- shell and file-edit tool interactions;
- active-turn steering or second-input behavior;
- interruption;
- upstream connection retry and retry exhaustion;
- same-process follow-up after a transport failure; and
- idle native session/thread resume in a new process.

Each test drives a fresh pinned harness against a loopback model it scripts one request at a time:
it asserts the markers of each upstream request (tool roster, system prompt or instructions, last
user text, tool-result ids and content, thinking/reasoning echo, steer position, resume transcript,
`prompt_cache_key`), answers with a built stream, and asserts the native frames and workspace
effects, rather than requiring every generated ID, timestamp, progress packet, or chunk boundary to
be identical. Provider differences remain visible:

- Claude and Codex expose different native control and lifecycle frames.
- With `CLAUDE_CODE_MAX_RETRIES` bounded, Claude retries a stream lost before or after visible text
  as a non-streaming request.
- Codex emitted retry notices and eventually a failed turn after repeated losses.
- Codex `turn/steer` requires `expectedTurnId` and joins the running turn.
- Steering, queued input, interruption, and resume use provider-native mechanisms and outcomes; they
  are not assumed to be equivalent merely because both providers have a related operation.

These observations are constraints on the adapter seam, not a license to manufacture common
semantics the providers did not demonstrate.

## What the shared seam may own next

The seam is one bidirectional gRPC stream per attachment, `Attach`, over session-scoped
commands (`Open`, `Input`, `Interrupt`, `Shutdown`, `Detach`) and sequenced events; the contract
is [`../runner/SPEC.md`](../runner/SPEC.md). It exposes only behavior the scripted tests prove with
the real binaries, and its own tests are one interaction script per scenario run against both
harnesses, so a caller never switches on the provider. There is no steer verb: both harnesses take
an input during a turn into that turn, so one `Input` covers both. The seam does not emulate
retry, redispatch an uncertain input, or present an unsupported operation as successful; provider
ids and frames stay available as `Native` events for correlation.

The shared driver protocol is an internal stdio boundary. It is distinct from the browser-facing
Agentplane API and does not decide Thread naming, archive presentation, timeline UX, or HTTP/SSE/WebSocket
resource design.

## Capture evidence contract

When the live probe is run, it must continue to preserve:

- native frames in both directions;
- complete payloads and framing boundaries;
- file order within each transcript;
- provider-native request, session, thread, turn, item, and tool IDs;
- model request bodies and streamed response chunks;
- enough process-exit information to diagnose a failed run.

The probe output is an investigation artifact kept outside Git; nothing consumes it mechanically,
and the behavioral assertions live in the scripted tests. Do not add routine byte lengths, hashes,
parsed-object copies, duplicate timestamps, sequence registries, or outer copies of provider IDs.
The ordered payload already supplies that information.

## Refreshing a pinned harness

When upgrading a Claude/Codex harness or adding coverage for a currently untested protocol area,
obtain the new pinned binary and run the live probe. Compare what the harness now sends with the
scripted expectations, then update the provider driver and scripted tests only for the behavior we
choose to support, together with the binary pin. The probe's logs stay outside Git.

## Product nouns after capture

These nouns can now be tested against observed provider behavior, but they are still product-level
concepts rather than fields to inject into native transcripts:

- **Thread**: durable user-visible interaction context;
- **Input**: an inbound message accepted by the Agentplane service;
- **Turn**: one provider execution bracket;
- **Runner**: the native process or adapter serving a Thread; and
- **Timeline event**: a later UI/service projection of native evidence.

The first shared seam should not require a central persistence model for these nouns. The standalone
service can introduce them at its own API boundary after the adapters are proven.

## Decisions enabled by the captures

Before expanding the seam, use the scripted tests to settle only the decisions needed
for the next slice:

- which native operations map cleanly to `submit`, `interrupt`, and `resume`;
- how each provider reports admission, progress, completion, failure, and process survival;
- whether a provider's active second input is queued, steered, rejected, or otherwise observable;
- which resume identifiers and native state must be supplied to a replacement process; and
- which provider-specific events must remain explicit instead of being collapsed into a common enum.

If a behavior is unsupported or supported differently, preserve that result in the adapter contract.
Do not add a generic state machine, retry policy, neutral operation projector, or timeline schema to
make the matrix appear symmetric.

## Separate follow-up work

Post-capture adapter work must remain separate from:

- the Agentplane REST/OpenAPI and SSE/WebSocket API;
- Thread/Input/Turn persistence and any PostgreSQL schema;
- Kubernetes/Agent Sandbox lifecycle and Pod replacement;
- bridge reconnect cursors, leases, fencing, or uncertain-input recovery;
- authorization, credentials, approvals, or subscription adapters; and
- conversation UI and Haku Console integration.

Those layers may consume the shared seam later, but none is required to define it or to reinterpret
the raw capture evidence.
