# Agent operations, approvals, and access

Status: **the v0 ActionRequest lifecycle and standalone Action Service are implemented; Action
schema, Executor wiring, and delivery remain explicit design gates.** The authoritative dependency
map and acceptance criteria are in [`task_dag.md`](task_dag.md).

## Accepted vocabulary and decisions

- **Action**: a named, versioned capability definition. Its exact schema/ownership is still the
  `AS` design gate.
- **ActionRequest**: one immutable caller intent to invoke an Action with structured arguments.
- **Decision**: an authorization disposition over one ActionRequest.
- **Execution**: the at-most-one concrete run created after an allow Decision.
- **Executor**: the adapter/process that performs that run. Its selection and boundary are still the
  `EW` design gate.
- **Delivery**: the pending/Decision/Execution event returned to the originating Thread. Its durable
  path is still the `DEL` design gate.

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
schema and is the canonical state owner for ActionRequests, Decisions, Executions, state events, and
pending-decision outbox references. The integration app/BFF and future notification surfaces are
clients.

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
- Credential-shaped keys are recursively redacted from caller/operator projections, execution
  results/errors, and pending outbox payloads; the present heuristic is not yet an Action definition
  sensitivity contract.

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

Withdrawal, notification delivery, originating-Thread delivery, adapter-specific reconciliation,
and cancellation after dispatch are not implemented by this slice.

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
- stable action/capability name and versioning, including how requests pin a definition;
- parameter representation and validation;
- stable result and error schema;
- sensitivity/redaction annotations for review, persistence, delivery, logs, and replay;
- compatibility and evolution rules; and
- definition ownership.

**Recommendation:** start with static, code-owned definitions beside code-owned adapters, validated
at startup. Use a small JSON-compatible typed contract unless the first adapter demonstrates a need
for full JSON Schema. Service configuration and a registry remain alternatives, not P0 machinery.

**Required evidence:** one hand-authored definition plus request/result/error transcript; positive
validation and offline replay; negative tests for unknown name/version, malformed parameters,
extra/missing/wrong-type values, incompatible definition evolution, malformed result/error, and
sensitive data appearing in any projection or log. Replay must not execute a backend.

## Open gate: Executor wiring contract (`EW`)

Before the echo fixture is replaced or supplemented, decide and test:

- how action name/version selects an Executor;
- capability/definition registration and duplicate/missing/incompatible startup failure;
- adapter/backend configuration and startup validation;
- in-process versus separate worker/process, justified by the first adapter's failure and isolation
  needs;
- the credential and Kubernetes ServiceAccount boundary;
- dispatch transport and how results/events return to the Action Service;
- exactly-one claim, no-retry, and unknown-outcome semantics across process/network loss;
- request and backend idempotency-key behavior;
- executor health and capability discovery; and
- one concrete first adapter acceptance fixture.

**Recommendation:** keep the first adapter code-owned and in-process only if its transport can keep
credentials in the correct process and uphold the no-retry boundary. Otherwise use a separate
worker for that adapter; do not build a general worker/controller framework first.

**Required evidence:** after one allow, the selected configured backend receives exactly one intended
payload under the intended credential identity; duplicate submission/Decision/start paths do not
invoke it twice; safe success/failure returns through the service; ambiguous loss records unknown
without replay; invalid or unhealthy registration fails before dispatch. The concrete backend target
and credential owner require Rai's choice before implementation.

## Open gate: pending/result delivery (`DEL`)

The landed `action_outbox` row contains only request ID and capability for a new pending request. It
proves that a credential-safe durable notification reference can be written transactionally; there
is no outbox consumer, notification callback path, or originating-Thread delivery yet.

Settle and test:

- non-blocking receipt and later input to an idle, active, or resumed Thread;
- outbox claim/acknowledgement and process-restart replay;
- redacted pending/Decision/result/error envelopes;
- withdrawal before Execution starts;
- duplicate/stale notification callbacks through the same human DecisionProvider; and
- agent-visible treatment of `execution_unknown` and any adapter-specific status reconciliation.

Standing grants remain separate objects owned by access/grants machinery. They are not an alternate
Execution count or an Action definition field.

## Authority boundaries

- **Action Service:** canonical lifecycle/state owner and access check.
- **DecisionProvider:** issues a final allow/deny; v0 is the human operator path.
- **Executor:** owns backend-specific invocation and result translation, never policy reinterpretation.
- **Integration app/BFF:** authenticates the browser/operator and calls the operator API; it does not
  duplicate Action state.
- **Agentplane runtime:** owns Sandbox/Thread lifecycle and eventually delivers a product event to a
  Thread; it does not own every external protocol.
- **Trajectory store:** may preserve larger evidence by reference but cannot bypass Action Service
  read authorization.
- **Notification adapter:** delivers a redacted reference and returns operator intent through the
  same DecisionProvider; it is not a second authority.

## Deferred

- MCP registry or universal protocol translation;
- capability matrices, general Agent privileges, and cross-agent permissions;
- standing-grant design inside the ActionRequest lifecycle;
- LLM DecisionProvider and cryptographic Decision signatures;
- dynamic definition authoring/registry;
- production executor implementation in this docs-only change; and
- broad external-access policy beyond the first concrete adapter.
