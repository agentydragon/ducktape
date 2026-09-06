# Agentplane task DAG

This is the authoritative project overview for Agentplane. It records landed behavior as evidence and
keeps only work with a current user-visible outcome or a named design gate in the active path. Edges
are real dependencies; packages without an edge may proceed independently. Labels mean:

- **P0 behavior**: the next user-visible behavior;
- **needed support**: implementation needed to prove that behavior;
- **observed evidence**: already landed or measured; and
- **deferred**: deliberately outside the current slice.

## Current truth on `devel`

**Observed evidence — workload authentication and LLM ingress landed.** PR
[#5685](https://github.com/agentydragon/ducktape/pull/5685) added the generic
`authenticatedWorkloadToken` credential source. PR
[#5696](https://github.com/agentydragon/ducktape/pull/5696) added the shared immutable
`SandboxPrincipal` resolver. PR [#5698](https://github.com/agentydragon/ducktape/pull/5698) added and
wired the independently deployable authenticated LLM ingress. Staging runners present only the
`agentplane-credential-agentplane-workload` placeholder; central egress substitutes the authenticated
Pod-bound bearer for the selected destination, which resolves the live Sandbox and forwards
provider-native traffic to LiteLLM with a server-held virtual key. The compatibility audience is
still `agentplane-egress`; `agentplane-workload` remains a possible coordinated rename, not missing
P0 behavior. See [`../docs/workload_authentication.md`](../docs/workload_authentication.md).

**Observed evidence — the standalone Action Service landed.** PR
[#5700](https://github.com/agentydragon/ducktape/pull/5700) made the service the PostgreSQL owner of
`ActionRequest`, `Decision`, `Execution`, state events, and a pending-decision outbox reference. The
current caller envelope accepts exactly a 1–200 character `idempotency_key`, a 1–240 character
`capability`, a JSON-object `arguments`, and optional JSON-object `origin`/`correlation`; extra
top-level fields are rejected, and origin/correlation are untrusted provenance. Workload callers can read their own redacted records; the operator surface can read all
and issue an expected-version, idempotent human allow/deny Decision. Allow auto-dispatches exactly
one Execution; there are no blind retries, and unsafe restart state becomes `execution_unknown`.

The only accepted capability is `agentplane:v0.echo`. `EchoExecutor` returns
`{"echo": <arguments>}` in-process and exists only to prove the coordinator seam, redaction,
single-execution claim, and recovery behavior. It is not a production action definition, backend,
MCP adapter, HTTP adapter, worker protocol, or credential-bearing executor. No real backend is wired
because the Action schema and Executor wiring contracts below have not been decided or tested.

**Observed evidence — launch presets landed.** PR
[#5648](https://github.com/agentydragon/ducktape/pull/5648) landed the app-owned `SandboxPreset` and
`ThreadPreset` first slice, including `public-coder`, runner initialization, UI selection, and the
manual live acceptance target. Broader capability profiles remain deferred; see
[`../docs/launch_presets.md`](../docs/launch_presets.md) and [`profiles.md`](profiles.md).

**Active review — egress rules API boundary.** PR
[#5701](https://github.com/agentydragon/ducktape/pull/5701) makes
`http://agentplane-egress.agentplane-staging.svc.cluster.local/v1/rules` an ordinary destination:
normal policy and exact workload-placeholder substitution, then independent destination bearer
validation through `SandboxPrincipalAuthenticator`. Service port 80 targets a separate API listener
in the same process/Pod; port 8888 remains the forward proxy. `RulesProjection` shares the enforcement
index and the redacted response contract. No local-dispatch branch or new credential mode is needed.

## DAG

```mermaid
flowchart TB
    classDef completed fill:#dcfce7,stroke:#15803d,color:#14532d,stroke-width:2px
    classDef active fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a,stroke-width:3px
    classDef decision fill:#ffedd5,stroke:#c2410c,color:#7c2d12,stroke-width:2px,stroke-dasharray:5 3
    classDef future fill:#f3f4f6,stroke:#6b7280,color:#374151
    classDef milestone fill:#ede9fe,stroke:#6d28d9,color:#4c1d95,stroke-width:2px

    F0["Observed evidence<br/>Sandbox + runner + app + trajectories"]:::completed
    AUTH["Observed evidence<br/>workload-token substitution + SandboxPrincipal"]:::completed
    LLM["Observed evidence<br/>authenticated LLM ingress"]:::completed
    ACTION0["Observed evidence<br/>standalone Action Service + human Decision path<br/>fixture echo only"]:::completed
    PRESETS["Observed evidence<br/>launch presets first slice"]:::completed

    AS["Action schema contract<br/>definition, name/version, params, result/error,<br/>redaction and evolution"]:::decision
    EW["Executor wiring contract<br/>registration, dispatch, credentials, delivery,<br/>claim/idempotency/health + first adapter"]:::decision
    DEL["Pending/result delivery contract<br/>outbox drain, Thread event, notification,<br/>withdrawal and unknown reconciliation"]:::decision
    ACTION1["P0 behavior<br/>one real named Action executes once<br/>and returns a safe result"]:::active

    ER["Active review #5701<br/>egress rules boundary + Service DNS transition"]:::active
    DEDUPE["Needed support, independent<br/>shared FastAPI/auth setup dedupe"]:::active
    T3["P0 behavior, independent<br/>trajectory search and lookup"]:::active
    PR["P0 behavior, independent<br/>proxy rollout survivability"]:::active

    BB["Deferred decision<br/>BuildBuddy hosted-run credential boundary"]:::future
    DT["Deferred<br/>driver-provided declarations/background control"]:::future
    AG["Deferred<br/>durable Agent identity + cross-agent read policy"]:::future
    PROD["Milestone<br/>production-capable governed action execution"]:::milestone

    F0 --> ACTION0
    AUTH --> LLM
    AUTH --> ACTION0
    ACTION0 --> AS
    ACTION0 --> EW
    ACTION0 --> DEL
    AS --> ACTION1
    EW --> ACTION1
    DEL --> ACTION1
    ACTION1 --> PROD

    AUTH --> ER
    AUTH --> DEDUPE
    F0 --> T3
    F0 --> PR
    ER -. independent cleanup .-> PROD
    DEDUPE -. independent support .-> PROD
    T3 -. independent product work .-> PROD
    PR -. independent reliability .-> PROD

    AS --> DT
    EW --> DT
    ACTION1 --> AG
```

The critical path to production action execution is `ACTION0 -> AS + EW + DEL -> ACTION1`. Egress
introspection cleanup, shared FastAPI/auth deduplication, trajectory search, and proxy survivability
can proceed without waiting for those gates. Their independence must not be described as evidence
that the current echo-only Action Service can execute production work.

## Named gates and acceptance evidence

### `AS` — Action schema contract

**P0 behavior:** a caller can submit one stable, reviewable, versioned Action whose parameters are
validated before a Decision or dispatch, and whose result/error can be safely replayed.

**Needed support / decisions:**

1. Define **Action** as the code-owned capability definition and **ActionRequest** as one immutable
   invocation of that definition. Keep the existing request lifecycle and one-logical-intent model.
2. Choose a stable capability/action name plus definition version. Decide whether the version is in
   the name or a separate required field, and what identity participates in idempotency comparison.
3. Define parameter representation and validation. **Recommendation:** begin with a deliberately
   small JSON-compatible typed contract compiled into the service/adapters; use JSON Schema only if
   the first real adapter needs features the smaller contract cannot express.
4. Define the result and stable error envelope, including which backend/provider details are safe to
   persist and return.
5. Define sensitivity annotations and recursive redaction for inputs, results, errors, Decision
   views, events, logs, and replay fixtures. Name what is reference-only versus reviewable content.
6. Define compatibility/evolution: whether old definitions remain executable, replay-only, or
   rejected; how a request pins its definition; and which changes are breaking.
7. Choose definition ownership. **Recommendation:** static code-owned adapter definitions loaded at
   startup. Alternatives are service configuration or a registry; neither should be built until a
   current adapter or independent authoring workflow requires it.

**Acceptance evidence:** one hand-authored Action definition and request/result/error transcript;
positive validation/replay tests; negative tests for unknown name/version, missing/extra/wrong-type
parameters, incompatible evolution, malformed result/error, and sensitive values in every
projection/log path. Tests must prove replay uses the pinned definition without executing a backend.

### `EW` — Executor wiring contract

**P0 behavior:** one accepted and allowed ActionRequest selects exactly one healthy configured
Executor, crosses a defined credential boundary, and produces one durable result or explicit unknown
outcome without replay.

**Needed support / decisions:**

1. Define how `(action name, definition version)` selects an Executor and how capability/definition
   registration is validated at startup. Duplicate, missing, or incompatible registrations fail
   startup or request admission; they do not fall through at dispatch time.
2. Define adapter/backend configuration and validation, including what is static code/config and what
   may be changed without rebuilding.
3. Choose in-process execution versus a separate worker/process for the first adapter, and record the
   failure/isolation property that justifies the choice. **Recommendation:** use an in-process,
   code-owned adapter only if its SDK/transport can uphold the credential and no-retry boundary;
   otherwise choose a separate worker before adding a generic worker framework.
4. Define the credential and Kubernetes ServiceAccount boundary. State which process may receive a
   real credential, how central egress or native workload identity is used, and what the Action
   Service itself must never possess.
5. Define dispatch transport and result/event delivery back to the Action Service, including how a
   worker proves which request it is completing.
6. Preserve the landed exactly-one claim: one Execution row, atomic claim before dispatch, no retry
   after dispatch may have begun, and `execution_unknown` after ambiguous loss. Define adapter-specific
   reconciliation only when an authoritative status lookup exists; reconciliation never starts a
   second effect.
7. Define idempotency-key behavior at request admission and at the backend boundary. A backend key
   may reduce duplicate effects but does not weaken the service's no-retry rule.
8. Define executor health and capability discovery as startup/readiness evidence, not a broad dynamic
   registry.
9. Select one concrete first adapter and write its acceptance fixture before implementation.
   Minimum evidence: the named Action validates, allow auto-dispatches once, the configured backend
   receives the exact intended payload and credential identity, duplicate Decision/start paths do
   not call it twice, success and safe failure are delivered, and ambiguous transport loss becomes
   unknown without retry.

`agentplane:v0.echo` remains explicitly fixture-only and cannot satisfy this gate.

### `DEL` — pending, result, and notification delivery contract

**P0 behavior:** submission remains non-blocking; a pending human Decision and the eventual
Decision/Execution result reach the originating Thread through one durable, redacted event path.

**Needed support:** define outbox draining and acknowledgement, idle/active/gone Thread delivery,
withdrawal before execution, notification deep links/callback idempotency, sensitive payload
projection, and what an agent receives for `execution_unknown`. The landed outbox row proves only a
safe pending reference exists; no consumer currently delivers it.

**Acceptance evidence:** a scripted replay covering submit -> pending -> allow/deny -> one execution
or no execution -> later Thread input, including process restart and duplicate notification callback.

## Preserved decisions

These are observed product decisions and must not be reopened by the schema or wiring gates:

- `ActionRequest` is one logical intent with an invariant request shape.
- `Decision` and `Execution` are separate durable records.
- One ActionRequest may create at most one Execution.
- A final allow Decision may auto-dispatch; there is no universal agent `commit` step.
- The v0 DecisionProvider is human/operator-backed.
- Dispatch is never blindly retried; ambiguity becomes `execution_unknown`.
- Caller-own and operator-all reads remain the v0 access scope, with credential-shaped data redacted.

## Deferred

- capability matrices or a broad Agent identity/privilege framework;
- MCP registry, dynamic action marketplace, standing grants, and cross-agent permissions;
- production executor implementation in this planning PR;
- per-destination workload audiences until recipient isolation is required;
- broad profiles beyond the landed launch-preset slice; and
- cryptographic Decision signing until Decisions cross a boundary that requires it.
