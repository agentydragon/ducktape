# Agent operations, approvals, and access

Status: **the v0 ActionRequest lifecycle and standalone Action Service are implemented; Action
schema, Executor wiring, and delivery remain explicit design gates.** The authoritative dependency
map and acceptance criteria are in [`task_dag.md`](task_dag.md).

## Accepted vocabulary and decisions

- **Action**: a stable, namespaced capability definition such as `github.get_file`. Its exact
  schema/ownership is still the `AS` design gate; no public `action_version` is required.
- **ActionRequest**: one immutable caller intent to invoke an Action with structured arguments.
- **Decision**: an authorization disposition over one ActionRequest.
- **Execution**: the at-most-one concrete run created after an allow Decision.
- **Executor**: the adapter/process that performs that run. Its selection and boundary are still the
  `EW` design gate.
- **Action events/API delivery**: the durable pending/Decision/Execution history and query surface
  exposed by the Action Service. Delivery into an originating Agent/Thread is a later Event &
  Notification Hub concern, not a prerequisite for the first MCP execution slice.

Preserve these already accepted decisions:

- `ActionRequest` is the product noun; do not create a separate “ask” or approval-only request type.
- A request is one logical intent and may produce at most one Execution.
- Decision and Execution are separate durable records.
- The request shape does not change based on whether review is automatic or human.
- A final allow Decision auto-dispatches; the agent does not self-approve or issue a universal
  `commit` step.
- The v0 DecisionProvider is human/operator-backed.
- There are no blind execution retries. If dispatch may have started and the result is unknown, the
  terminal projection is `execution_unknown` unless an adapter can reconcile through an
  authoritative status read without starting another effect.
- Caller-own and operator-all reads are the v0 scope. Cross-agent reads and standing grants are
  separate deferred products.

## Observed evidence on `devel`

PR [#5700](https://github.com/agentydragon/ducktape/pull/5700) landed an independently deployable
Action Service, not an in-process “Action Hub” inside the integration app. It owns its own PostgreSQL
schema and is the canonical state owner for ActionRequests, Decisions, Executions, and state events.
The v0 schema also retains a pending-decision outbox reference without request arguments; the first
MCP execution path uses durable Action events/API polling rather than a separate delivery queue. The
integration app/BFF and future notification surfaces are clients.

### Current ActionRequest contract

`POST /v1/action-requests` accepts exactly:

```json
{
  "idempotency_key": "caller-stable-key",
  "capability": "agentplane:v0.echo",
  "arguments": { "text": "hello" },
  "origin": {},
  "correlation": {}
}
```

- `idempotency_key` is a required 1–200 character string, unique per authenticated caller. Reusing
  it with the same envelope returns the original request; reusing it with a different envelope
  conflicts.
- `capability` is a required 1–240 character string and must be advertised by the configured
  Executor.
- `arguments` is a required JSON object but has no capability-specific schema yet.
- `origin` and `correlation` are optional JSON objects stored only as untrusted provenance. They do
  not establish Sandbox, Thread, Agent, owner, role, or operator authority.
- Extra top-level fields are rejected.
- Workload identity comes only from the shared destination-side `SandboxPrincipal` resolver. Two
  Pods using the same ServiceAccount remain distinct callers through their live Sandbox UIDs.
- Credential-shaped keys are recursively redacted from caller/operator projections and execution
  results/errors; the present heuristic is a projection rule, not an Action field or a complete
  backend schema.

### Current Decision and Execution behavior

The separate operator route issues a human `allow` or `deny` with expected-version and idempotency
checks. A winning allow creates one Execution row and schedules automatic dispatch. The store
atomically moves the only Execution from `pending_dispatch` to `dispatching` before invoking the
Executor. A restart resumes only work that provably remained `pending_dispatch`; `dispatching` or
`running` work becomes `execution_unknown`. Executor exceptions are projected through a stable safe
classification and are not retried.

The current lifecycle proven by tests is:

```text
decision_pending -> allowed -> dispatching -> running -> succeeded | failed | execution_unknown
decision_pending -> denied
```

Withdrawal, originating-Agent/Thread delivery, adapter-specific reconciliation, and cancellation
after dispatch are not implemented by this slice. Executor liveness and orphan recovery are now
specified separately in [`../docs/executor_liveness.md`](../docs/executor_liveness.md); a coordinator
restart no longer unconditionally declares dispatching/running work unknown, and stale leases are
the recovery signal.

### Fixture-only executor

`EchoExecutor` advertises only `agentplane:v0.echo` and returns:

```json
{ "echo": { "...": "the submitted arguments" } }
```

It runs in the Action Service process and performs no external effect. It proves request admission,
human Decision, automatic single dispatch, result projection, redaction, and recovery. It does not
prove a production Action definition, schema validation, backend configuration, credential boundary,
worker transport, capability discovery, health reporting, MCP integration, or real result delivery.
It must remain described and named as a fixture.

## Open gate: Action schema contract (`AS`)

Before a real adapter is implemented, decide and test:

- Action versus ActionRequest: the definition is not the invocation;
- stable group/action key and catalog evolution behavior; no public `action_version` is required, and
  execution re-checks the current executor/tool schema before refusing incompatible arguments;
- parameter representation and validation;
- stable result and error schema;
- redaction and projection rules for review, persistence, delivery, logs, and replay;
- compatibility and evolution rules; and
- definition ownership.

**Recommendation:** keep the ActionGroup-to-executor and MCP-server/tool bindings in runtime
configuration such as a reviewed YAML file, so backend/account changes do not require an image roll.
Use the executor's live tool schema for admission/execution checks; use a small JSON-compatible typed
contract unless the first adapter demonstrates a need for full JSON Schema.

**Required evidence:** connect the configured GitHub MCP server as the user's account, mirror its
catalog, auto-allow safe public-repository reads, and prove with an acceptance test that an Agent can
invoke one read Action and receive a safe result. Include negative tests for unknown group/action,
malformed parameters, incompatible current tool schema, malformed result/error, and sensitive data
appearing in any projection or log.

## Open gate: Executor wiring contract (`EW`)

Before the echo fixture is replaced or supplemented, decide and test:

- how group/action identity selects an Executor;
- capability/definition registration and duplicate/missing/incompatible startup failure;
- adapter/backend configuration and startup validation;
- ActionGroup/catalog hierarchy and how child Actions are discovered and admitted;
- in-process versus separate worker/process, justified by the first adapter's failure and isolation
  needs;
- an executor boundary compatible with MCP-backed adapters without making MCP the Agent-facing API;
- the credential and Kubernetes ServiceAccount boundary;
- dispatch transport and how results/events return to the Action Service;
- exactly-one claim, no-retry, and unknown-outcome semantics across process/network loss;
- request and backend idempotency-key behavior;
- executor health, executor heartbeat, and per-Execution lease/heartbeat behavior;
- bounded progress/status observations for long-running executions; and
- one concrete first adapter acceptance fixture.

**Recommendation:** keep the first adapter code-owned and in-process only if its transport can keep
credentials in the correct process and uphold the no-retry boundary. Otherwise use a separate
worker for that adapter; do not build a general worker/controller framework first.

**Required evidence:** after one allow, the selected configured backend receives exactly one intended
payload under the intended credential identity; duplicate submission/Decision/start paths do not
invoke it twice; safe success/failure returns through the service; ambiguous loss records unknown
without replay; invalid or unhealthy registration fails before dispatch. The concrete backend target
and credential owner require Rai's choice before implementation.

### Action groups, MCP discovery, and backend ownership

The Agent-facing catalog should be hierarchical rather than one flat list. An `ActionGroup` is the
discovery and ownership unit, with child Actions addressed by a stable namespaced key such as
`github.create_issue`. A group binds to one executor/backend configuration and carries the group-level
description, account/credential ownership summary, availability, and discovery status. An Action
under the group carries the tool name, description, input schema, and invocation metadata. The group
may be backed by MCP, HTTP, hostexec, or another executor protocol; MCP server and ActionGroup are
related concepts, not the same required abstraction.

The executor contract should be protocol-neutral at the Action Service boundary, but it must be able
to host an MCP-backed adapter. By default, the adapter mirrors the tools returned by MCP `tools/list`,
including tool schemas and descriptions, rather than requiring every GitHub-sized tool catalog to be
hand-authored. Mirroring is discovery, not unconditional authorization: group configuration and the
DecisionProvider still govern whether a discovered Action can execute.

When an MCP server supports `notifications/tools/list_changed`, the adapter should refresh the group
catalog and emit the corresponding catalog-change evidence. Correctness must not depend on receiving
that notification: startup, periodic, and on-demand refresh remain valid, and the REST catalog/call
path can be used when an Agent needs to inspect or invoke an Action after a missed notification.

There is no public `action_version` requirement. The request stores the stable group/action identity
and arguments. At execution, the adapter re-checks the current executor/tool schema and refuses the
request safely if the arguments or binding are no longer compatible; a catalog change must never be
silently remapped to another Action.

For an MCP-backed Action, the Action Service/executor owns the connection to the configured MCP server
and initiates the server-side `tools/call`; the Agent or native harness does not initiate the effect
through an attached MCP client. The adapter maps the MCP tool's input, result, and error into the
Action definition and Execution envelope, while preserving the Action Service's approval, claim,
no-retry, and redaction rules.

The initial Agent-facing interface remains Haku-owned rather than being forced to look like MCP. This
allows pending approval, auto-approval, result delivery, and progress semantics to be designed as
first-class Action behavior even when a backend happens to speak MCP. A future deployment may expose
one or more MCP servers directly to harnesses, but dynamic MCP attachment/tool discovery is not a
prerequisite for the first Actions API.

Authentication and credential ownership are backend-specific. A connected server may use a user's
privileged GitHub identity, future Gmail or Google Calendar OAuth, or hostexec-style machine access.
The executor definition and Agent-visible description must identify that boundary and warn against
using privileged access for work the Agent can perform itself. Real credentials remain in the owning
backend/worker boundary, not in the harness or ActionRequest.

An executor may report bounded progress snapshots or output observations while an Execution is
running. Progress is not a second Execution and does not relax the rule that an ambiguous dispatch
is `execution_unknown` rather than an invitation to retry. A hostexec-style adapter may expose
authorized process state and output-so-far reads, subject to the same redaction and access checks as
terminal results.

Executor liveness has two layers:

- an executor heartbeat says that the adapter/worker is available to accept or observe work; and
- an Execution heartbeat/lease says that a started Execution still has a live coordinator.

If an Execution lease expires, the Action Service may retire the coordination record with a stable
`execution_unknown` outcome and an executor-lost reason. A missed heartbeat cannot prove that an
external effect stopped, so it must never trigger a second `start`. If the backend offers an
authoritative status lookup, a later observation may reconcile the existing Execution without replay.
Late completion messages must be authenticated and tied to the existing Execution; they do not create
a new claim.

Decision providers receive trusted caller context from the Action Service, including the authenticated
caller principal and a verified Agent identity when the deployment can resolve one. They must not
infer Agent identity from `origin`, `correlation`, or other caller-controlled fields. If only the
Sandbox principal is available, that limitation is explicit in the decision context rather than
silently filled with an unverified Agent name.

## Open gate: decision and Action-state contract (`DEL`)

**P0 behavior:** submission remains non-blocking; the Action API and durable Action events expose a
pending human Decision and the eventual Decision/Execution result with bounded provider-authored reason
evidence. Originating-Agent/Thread notification is a later integration node, not a prerequisite for
proving that an Agent can use an Action backed by MCP.

**Landed:** provider-outcome aggregation and deny-dominant finalization, and duplicate/stale
callbacks across the human and auto-provider Decision routes — PR
[#5732](https://github.com/agentydragon/ducktape/pull/5732), summarized in
[`async_approvals.md`](async_approvals.md).

Settle and test:

- redacted pending/Decision/result/error envelopes with provider reason evidence; and
- withdrawal before Execution starts and Agent/API-visible treatment of `execution_unknown` and any
  adapter-specific status reconciliation.

**Landed:** durable Action event append/query with restart recovery and cursor-based
`after_sequence` polling, so a caller can resume from the last sequence it already has and repeated
reads are a no-op. See README.md's "Delivery: polling, not an outbox".

The Action Service's event history is the source of truth. A separate outbox is not required for this
slice; cross-service push delivery belongs to the later Event & Notification Hub or a concrete
executor-worker boundary, and either is expected to consume the Action event sequence directly rather
than the now-unused `action_outbox` table.

Standing grants remain separate objects owned by access/grants machinery. They are not an alternate
Execution count or an Action definition field.

## Authority boundaries

- **Action Service:** canonical lifecycle/state owner and access check.
- **DecisionProvider:** issues an authoritative provider outcome; v0 is the human operator path,
  with modular auto-approval providers using the same request shape and lifecycle rather than a
  second authority path. Each outcome is `allow`, `deny`, or `no_opinion` and carries a bounded
  provider-authored reason code/description. The Action Service aggregates those outcomes and
  commits the final lifecycle transition; a provider explanation is evidence, not private chain of
  thought.
- **Executor:** owns backend-specific invocation and result translation, never policy reinterpretation.
- **Integration app/BFF:** authenticates the browser/operator and calls the operator API; it does not
  duplicate Action state.
- **Event & Notification Hub:** later owns subscription matching, external-event ingestion, batching,
  rate limits, and delivery into an Agent/Thread ingress; it does not execute Actions or decide them.
- **Agentplane runtime:** owns Sandbox/Thread lifecycle and exposes the ingress used by the Hub; it
  does not own every external protocol.
- **Trajectory store:** may preserve larger evidence by reference but cannot bypass Action Service
  read authorization.
- **Notification adapter:** delivers a redacted reference and returns operator intent through the
  same DecisionProvider; it is not a second authority.

## Deferred

- generic MCP registry/universal protocol translation and direct harness MCP exposure; MCP-backed
  adapters remain in the Executor wiring gate;
- capability matrices, general Agent privileges, and cross-agent permissions;
- standing-grant design inside the ActionRequest lifecycle;
- LLM DecisionProvider and cryptographic Decision signatures;
- dynamic definition authoring/registry;
- production executor implementation in this docs-only change; and
- broad external-access policy beyond the first concrete adapter.
