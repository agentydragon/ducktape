# Agentplane experiments

Status: **native drivers, the live probe, and the scripted harness tests are in place; the thin
shared adapter seam is next**.

The native capture work is no longer an open discovery task. The Claude/Codex drivers live under
[`../native/`](../native/), the live-capture probe under [`../capture/`](../capture/), and the
scripted per-provider tests under [`../harness_tests/`](../harness_tests/): a shared loopback model
endpoint in `../harness_tests/scripted_upstream.py`, with SSE builders, typed request parsers, frame
assertions, and the tests beside each provider.

The purpose of this document is therefore to record observed provider evidence and the experiments
that should inform the next shared-protocol slice. It is not a request to recapture the same
baseline behaviors or to build a generic compatibility framework.

## Evidence delivered

For both Claude and Codex, the scripted tests cover:

- launch and native handshake;
- baseline streaming and terminal output;
- shell command and file-edit tool lifecycles, including externally visible effects;
- active-turn steering or second-input behavior;
- interruption and native terminal state;
- upstream connection retry and retry exhaustion;
- follow-up input after a transport failure; and
- idle native session/thread resume after replacing only the idle process.

Each live probe run preserves native stdin/stdout/stderr, complete upstream request bodies, streamed
response chunks, and controlled loss markers. It intentionally excludes headers, cookies,
credentials, OAuth state, and private user data. Its output stays outside Git as reference material
for authoring or repairing a script. The scripted tests assert the scenario-relevant upstream request
markers, lifecycle, output, process survival, and workspace behavior of a fresh pinned harness.

## Current verification gate

The focused tests are:

```sh
bbr test //x/agentplane/harness_tests/...
```

They launch the pinned Claude and Codex binaries against a loopback model endpoint the test drives
one request at a time: take the upstream request and assert its markers, answer with a built stream
(truncatable after a named packet to simulate connection loss; standing rules absorb retry storms),
then assert the native frames. Steps are synchronized by native protocol checkpoints and request
hand-off, not by sleeps or exhaustive packet equality. They tolerate generated IDs, timestamps,
optional metadata, extra progress events, and provider-specific chunk boundaries where those do not
change the tested behavior.

The tests verify, per provider:

- the native handshake and terminal outcome;
- the relevant tool, command, file, steer, queue, or interrupt lifecycle;
- retry/error/give-up behavior for connection-loss cases;
- same-process survival and follow-up recovery where captured;
- native continuity for idle resume; and
- the expected assistant output or workspace effect.

Request bodies are checked for scenario-relevant markers, not literal equality of every volatile
field. The live probe is available for investigation when needed. A provider-specific result
must be recorded as **Proven**, **Supported differently**, **Unsupported**, or
**Environment-blocked**, rather than normalized into a false common success.

## Captured provider observations

The pinned harnesses show these constraints:

- Claude uses newline-delimited stream/control JSON. With `CLAUDE_CODE_MAX_RETRIES` bounded, its
  pinned version retries a stream lost before or after visible text as a non-streaming request.
- Codex app-server uses newline-delimited JSON-RPC-shaped messages. Its captured version emitted
  retry notices and eventually reported a failed turn after repeated upstream losses.
- A Claude active-turn second input is observed as native input behavior; it must not be called
  steering unless the provider's native evidence supports that interpretation.
- Codex exposes explicit `turn/steer` and `turn/interrupt` operations in the scenarios where they
  are supported; `turn/steer` requires `expectedTurnId` and joins the running turn.
- Codex switches to code mode (one JS `exec` tool) for model ids in its built-in catalog; the tests
  use an unknown model id so the classic function-call shape is what gets asserted.
- Claude's session-title model call is suppressed by `--name`.
- Both harnesses run with thinking/reasoning on; the tests assert the thinking signature or
  encrypted reasoning is echoed back upstream.
- Idle resume uses Claude `--resume` or Codex `thread/resume`, not transcript replay or prompt
  redispatch.

These are observations from the currently pinned harnesses for adapter design. The bridge must not manufacture retry, steering,
acknowledgement, or successful completion semantics that the native process did not emit.

## Next post-capture experiments

The next focused work package is the thin shared stdio protocol and both provider adapters. Use the
scripted tests and real binaries to answer only what that package needs:

1. Which native start/resume, submit, interrupt, and event operations have stable behavioral
   equivalents?
2. What admission, progress, completion, failure, and process-survival evidence should the shared
   seam expose without hiding provider-specific details?
3. How should supported-differently or unsupported steering and queue behavior be represented?
4. Which native continuity identifiers must be retained to resume a replacement idle process?
5. What is the smallest adapter-level contract that can be proven by the scripted tests before
   introducing the standalone Agentplane service?

Do not add a neutral timeline, retry policy, central state machine, or persistence schema merely to
answer these questions. The live probe is the authority on provider behavior; the scripted tests are
the pinned contract.

## Refreshing harness evidence

The probe output is intentionally not a Git artifact. If a pinned harness version changes, or a new
protocol behavior needs coverage:

1. obtain the new pinned Claude or Codex harness;
2. run the live probe against the synthetic workspace and model endpoint;
3. compare what the harness now sends with the scripted expectations;
4. update the provider driver and scripted tests only for behavior we intend to pin down; and
5. update the harness pin.

Do not commit probe logs merely to make the repository self-contained; they are regenerated when a
review or investigation needs them.

## Historical capture invariants

These rules remain in force for any new probe scenario or scripted test:

- use real native stdin/stdout pipes, never PTYs, tmux, terminal scraping, or pane attachment;
- drain stderr and preserve bounded useful diagnostics;
- capture upstream model bodies through the minimal local HTTP server/proxy;
- use synthetic workspaces and deterministic tool inputs;
- synchronize steering and interrupt races with native events or explicit checkpoints, not sleeps as
  the only evidence;
- keep process exit status and relevant failure messages, without PID/signal chronology or
  Kubernetes identity;
- never blindly redispatch an input after uncertain process death; and
- treat unavailable binaries or unsupported provider operations as explicit results.

For connection-loss cases, cut one active upstream stream after a named complete partial-content
packet and before terminal completion. Keep the native process and stdio connection alive, restore
model-endpoint availability, and observe the provider's behavior. Start without tools so retries
cannot repeat side effects. This tests model-API transport behavior, not provider-native resume,
bridge reconnect, or Input redispatch.

## Probe output shape

A live probe run contains the complete native and upstream evidence:

```text
metadata.json
stdin.jsonl
stdout.jsonl
stderr.jsonl
llm-requests.jsonl
llm-responses.jsonl
```

It stays outside Git; the scripted tests have no recorded inputs or golden outputs. Native and
upstream files are UTF-8 text. File order is the ordering authority. Do not add routine hashes,
lengths, timestamps, manifest inventories, parsed mirrors, or custom DLP/promotion machinery.
Semantic expectations belong in the scripted tests.

## Deferred experiments

These remain outside the capture and adapter slice:

- multiple pending prompts and durable dequeue policy;
- active-turn process death and side-effect reconciliation;
- central-server reconnect or bridge-log replay;
- Pod replacement, Sandbox suspension, PVC lifecycle, or Service topology;
- Thread/Input/Turn persistence, PostgreSQL, and common timeline projection;
- leases, fencing, authentication, mTLS, credentials, approvals, or subscription adapters; and
- the standalone Agentplane API, conversation UI, and Haku Console integration.

They become worthwhile only after the shared seam is proven with the real captured behaviors and a
concrete product failure or decision requires them.
