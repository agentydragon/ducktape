# Agentplane architecture

Status: **focused proposal**.

Agentplane is a small bridge/controller for running native Claude Code and Codex harnesses in
replaceable Kubernetes workloads. Its first implementation goal is reliable native process driving,
not a general agent platform.

## Product boundary

The v0 product needs to:

- launch Claude Code and Codex without terminal emulation;
- send inputs through their native machine protocols;
- receive messages, tool calls/results, streaming output, interrupts, and steering where supported;
- retain the native session/thread identity needed for provider-native resume; and
- expose enough evidence to understand what the harness and upstream model actually did.

A later controller may add durable user Threads, a web timeline, Kubernetes reconciliation, and
multiple Agents. Those are not prerequisites for the native capture slice.

The native harness remains the agent loop. Agentplane does not replace Claude Code or Codex with a
home-grown direct-LLM loop in v0.

## Non-goals for the first implementation

Do not make these part of the initial bridge/capture task:

- a generic workflow or task DSL;
- a provider-neutral event schema before provider captures exist;
- PostgreSQL schemas or ORM models;
- an event-sourced UI timeline;
- Kubernetes/Agent Sandbox controller behavior;
- per-Sandbox Service creation or endpoint management;
- authentication, mTLS, grants, approvals, or credential delivery;
- subscriptions, GitHub/event adapters, or multi-Agent rooms;
- runtime-generation/fencing identities;
- automatic retry of an input after uncertain native delivery; or
- a direct LLM agent loop.

These are possible follow-up work, not design constraints on the capture experiment.

## Ownership

Keep one authoritative owner for each fact:

- **Kubernetes/Agent Sandbox** owns Sandbox, Pod, PVC, readiness, suspension, scheduling, and
  workload lifecycle.
- **The native harness** owns its native session/thread history, queue behavior, tool semantics,
  execution state, and native resume behavior.
- **The bridge** owns child-process supervision, native protocol I/O, and capture of the exchanges it
  observes.
- **A future central service** may own product Thread/Input/Turn records, recovery decisions, and a
  user-visible timeline. It should consume Kubernetes and bridge observations rather than pretend to
  own their underlying state.

PostgreSQL is a reasonable future default for central product state and evidence. It is not a
universal authority and is not required to prove the first native drivers.

## Workload shape

The first workload contains one bridge process and one selected native harness process. Use ordinary
stdin/stdout pipes and an independently drained stderr pipe. Do not use PTYs, tmux, pane scraping,
prompt detection, or `kubectl exec` as the integration path.

A bridge may supervise more than one native turn over its lifetime. Avoid deliberate competing
bridge processes in one Pod, but do not require a particular Kubernetes restart policy. If a
container or Pod is replaced, the new process must use native resume rather than reconstructing
provider continuity from an Agentplane transcript.

The leading Kubernetes shape is:

```text
Sandbox -> Pod, PVC
       \
        optional separately managed Service
```

Agent Sandbox documentation supports composing a normal Service around Sandbox ports. The pinned
local controller manifests do not establish that the Sandbox CR creates such a Service. Service
selection, connection direction, endpoint stability, and control-channel security remain integration
questions for later experiments.

## Identity

Use natural identities until a concrete failure requires more:

- Kubernetes Pod UID identifies the concrete runner Pod;
- process start/exit observation identifies the bridge/native process instance;
- provider-native Claude session id or Codex thread id identifies native continuity; and
- a future product Thread id identifies user-visible context.

Do not inject a Sandbox-scoped runtime-generation ordinal merely to make records look formally
fenced. If stale writers or split-brain processes are observed later, design the smallest control
needed to address that failure.

## Native capture is the first vertical slice

The implementation lives under `x/agentplane/`, with explicit provider drivers in
[`../native/`](../native/) and the live probe in [`../capture/`](../capture/). A live capture
produces an inspectable bundle containing:

- ordered native frames in both directions;
- upstream model request bodies and streamed response chunks;
- useful stderr on failure;
- process exit status; and
- hand-authored expected behavior, including tool and workspace effects where relevant.

The live capture is investigation evidence kept outside Git; the scripted tests in
[`../harness_tests/`](../harness_tests/) carry the behavioral contract and commit no recordings.
Do not add redundant lengths, hashes, timestamps, parsed copies, manifest inventories, checksum files,
or a custom promotion/DLP system.

The upstream capture boundary must never serialize HTTP headers, cookies, environment variables,
OAuth state, or credentials. Use a synthetic workspace and ordinary repository secret checks plus a
small obvious-token guard.

Each real-harness test starts a loopback Anthropic/OpenAI upstream the test scripts one request at
a time, runs the real harness, drives its native protocol, and asserts both sides of the loop. This
is more valuable than a large offline artifact validator.

## Provider split

Claude and Codex are separate adapters in v0.

Claude uses newline-delimited stream/control JSON. The adapter must prove initialization, user-frame
submission, streamed assistant/tool output, terminal results, interrupt, active-turn input behavior,
and native resume where supported.

Codex app-server uses newline-delimited JSON-RPC-shaped messages. The adapter must prove
`initialize`/`initialized`, durable thread start/resume, turn start, item notifications, terminal
completion, `turn/steer`, and `turn/interrupt` where supported. It must answer server requests needed
by the tested scenario instead of assuming output is notifications only.

Provider-native ids remain inside the provider transcript. A neutral adapter contract should be
derived only after these scenarios produce useful captures for both providers.

## Minimal control flow

The v0 bridge flow is intentionally small:

```text
launch bridge
  -> launch selected native harness
  -> initialize native protocol
  -> send provider-specific scenario input
  -> read and record native frames
  -> record upstream model bodies/chunks
  -> return native terminal evidence
  -> optionally restart an idle child and invoke native resume
```

At every step, the driver should preserve the raw exchange and assert what it actually observed.
A write to stdin is not automatically provider admission. An interrupt request is not automatically
a completed interruption. A process exit is not automatically a clean failed turn.

## Resume and recovery scope

The first recovery claim is narrow:

- complete a no-tool turn;
- kill only the idle native child;
- start a new child with the provider's native resume mechanism; and
- prove recovery of a nonce in model context.

Keep workspace survival as a separate assertion. Do not call bridge-journal replay or prompt resend
native resume.

The runner adds reconnect from a cursor, runner replacement with the loss reported, and
`InputUncertain` for an input whose admission it cannot settle
([`../runner/SPEC.md`](../runner/SPEC.md)). Active-turn side-effect reconciliation, queue
survival, Pod replacement, and Sandbox suspension are the next experiments
([`runner_sandbox_and_app.md`](runner_sandbox_and_app.md)). Do not blindly resend an input whose
native admission is uncertain.

## Future central service

Once native captures establish a real intersection, a central service may add:

- durable product Threads and accepted Inputs;
- Turns and a user-facing timeline;
- a small provider-neutral bridge interface;
- recovery decisions and explicit uncertain outcomes;
- persistence of exact native evidence; and
- a deliberate headless API consumed by a conversation-style app.

The app may begin as a separate Agentplane client and may later be built into or hosted by Haku
Console. LLM-generated Thread names, name persistence, archive presentation, and similar product
behavior belong above the native bridge; they may use an explicit Agentplane metadata API if more than
one client needs them.

The common model should preserve links to native evidence but should not require every provider
feature to collapse into a lowest-common-denominator operation taxonomy.

A future central-to-runner channel may be central-initiated through a separately managed Service or
bridge-initiated. Do not freeze that decision before connection and Sandbox experiments. Likewise,
add authentication or fencing only when the deployment threat/failure model and observed behavior
justify it.

## Future policy and credential layers

If secure HTTP egress or privileged tool calls are added later, keep the layers separate:

- Agentplane owns the live `Pod -> Sandbox -> Thread -> Agent` mapping and may expose a narrow internal
  identity-resolution endpoint;
- an eventual Access Controller owns allow/deny/`user_approval_required` policy decisions;
- a local fixed-operation proxy presents an audience-scoped Pod-bound token; and
- a trusted external gateway validates workload identity, executes only the authorized operation, and
  holds/substitutes real upstream credentials.

Prefer the gateway to verify the Kubernetes token before asking Agentplane to resolve the verified Pod
identity. Do not make Agentplane a general token introspection oracle unless a concrete requirement
justifies it. The conversation app—or a later Haku Console surface—should provide the explicit user
approval interaction without CLI copy/paste. Conservative defaults with a high explicit-approval
ceiling are a future product property, not a v0 bridge dependency.

This narrow egress/access-controller path must not be mistaken for the eventual general permission
model for private Haku. The current Ducktape-only lane can accept broad internet access plus the
scoped GitHub credential used by `agentydragon-agent`; that is a local operational convenience, not a
safe default for an agent with Rai's personal context. A later Agent Console track must separately
design permissions for tool execution, HTTP/API calls, credential handling, approvals, and resilient
Haku policy enforcement.

## Deferred product layers

Out of Agentplane v0:

- credential and consumer-OAuth ownership;
- stateless MCP authorization and approval escalation;
- subscription, schedule, GitHub, and external-event adapters;
- model capability catalogs and routing policy;
- Agent delegation and multi-Agent rooms;
- direct-LLM adapters;
- long-term retention/compaction policy; and
- detailed UI state and timeline projections.

These layers may send enveloped inputs to a future Agentplane controller. They should not be smuggled
into the native capture format.
