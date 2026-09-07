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
one Execution; there are no blind retries, and an ambiguous outcome becomes `execution_unknown`
through the bounded lease/recovery contract in [`../docs/executor_liveness.md`](../docs/executor_liveness.md).

The only accepted capability is `agentplane:v0.echo`. `EchoExecutor` returns
`{"echo": <arguments>}` in-process and exists only to prove the coordinator seam, redaction,
single-execution claim, and recovery behavior. It is not a production action definition, backend,
MCP adapter, HTTP adapter, worker protocol, or credential-bearing executor. The ActionGroup/Action
catalog discovery seam is landed in PR [#5731](https://github.com/agentydragon/ducktape/pull/5731),
synchronous deny-dominant DecisionProvider aggregation is landed in PR
[#5732](https://github.com/agentydragon/ducktape/pull/5732), and the executor heartbeat/lease
recovery contract is landed for this fixture seam in PR
[#5733](https://github.com/agentydragon/ducktape/pull/5733). No production backend is wired because
the remaining Action schema and production Executor wiring contracts below have not been fully tested.

**Observed evidence — first MCP-backed Executor landed.** PR
[#5753](https://github.com/agentydragon/ducktape/pull/5753) adds the tested
`McpActionGroupExecutor`: a stdio MCP adapter that mirrors `tools/list`, refreshes on notification
or interval, rechecks the live tool schema, initiates `tools/call`, and maps safe success/error/
unknown outcomes. The Action Service production composition still wires `EchoExecutor`; runtime
wiring, a remote streamable-HTTP staging fixture, and live Agent acceptance remain open.

**Observed evidence — launch presets landed.** PR
[#5648](https://github.com/agentydragon/ducktape/pull/5648) landed the app-owned `SandboxPreset` and
`ThreadPreset` first slice, including `public-coder`, runner initialization, UI selection, and the
manual live acceptance target. Broader capability profiles remain deferred; see
[`../docs/launch_presets.md`](../docs/launch_presets.md) and [`profiles.md`](profiles.md).

**Observed evidence — executor liveness and orphan-recovery contract landed for the in-process
fixture executor.** Executor-level health heartbeats, a per-Execution lease/heartbeat with bounded
expiry, `lease_expired`/`executor_lost` reason attribution, and authenticated late-completion or
authoritative-status reconciliation restricted to an Execution already `execution_unknown` resolve
`EW` item 6 and part of item 5. Dispatch is still in-process; items 2–4 and 7 remain open, while the
MCP adapter portion of item 9 is landed in #5753 and its production composition/remote acceptance
remain open; see
[`../docs/executor_liveness.md`](../docs/executor_liveness.md).

**Observed evidence — egress rules API boundary landed.** PR
[#5701](https://github.com/agentydragon/ducktape/pull/5701) made
`http://agentplane-egress.agentplane-staging.svc.cluster.local/v1/rules` an ordinary destination:
normal policy and exact workload-placeholder substitution, then independent destination bearer
validation through `SandboxPrincipalAuthenticator`. Service port 80 targets a separate API listener
in the same process/Pod; port 8888 remains the forward proxy. `RulesProjection` shares the enforcement
index and the redacted response contract. No local-dispatch branch or new credential mode is needed.

## DAG

```mermaid
flowchart TB
    classDef active fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a,stroke-width:3px
    classDef decision fill:#ffedd5,stroke:#c2410c,color:#7c2d12,stroke-width:2px,stroke-dasharray:5 3
    classDef future fill:#f3f4f6,stroke:#6b7280,color:#374151
    classDef milestone fill:#ede9fe,stroke:#6d28d9,color:#4c1d95,stroke-width:2px

    AS["Action schema contract<br/>stable identity, params, result/error,<br/>redaction and evolution"]:::decision
    EW["Executor wiring contract<br/>groups/catalog, dispatch, credentials, MCP compatibility,<br/>claim/idempotency/heartbeat + first adapter"]:::decision
    DEL["Decision/action-state contract<br/>provider aggregation, event/query API,<br/>reason evidence, progress, withdrawal, unknown"]:::decision
    MCP0["P0 behavior<br/>credentialless remote MCP Action<br/>real staging LLM acceptance"]:::active
    MCPAUTH["Deferred support<br/>credentialed MCP account<br/>OAuth + credential-broker boundary"]:::future
    CRED["Deferred decision<br/>static credential + binding design<br/>ownership, lifecycle, revocation"]:::future
    MCPACCEPT["Milestone<br/>rerunnable Action/MCP acceptance<br/>against the deployed stack"]:::milestone
    EID["Deferred support<br/>external Agent identity/auth<br/>static principal, not Thread"]:::future
    MCPAGG["Deferred support<br/>Agentplane MCP aggregator<br/>external harness/client compatibility"]:::future
    HOSTEXEC["Deferred adapter<br/>hostexec-backed Action execution"]:::future
    APPROVALUI["Deferred integration<br/>integration-app approval UI<br/>pending requests + decisions"]:::future
    RETIRE_AGENT["Deferred migration<br/>retire Haku Console Agent/<br/>conversation management"]:::future
    RETIRE_TOOLS["Deferred migration<br/>retire Haku Console tool-call/<br/>approval management"]:::future
    DEDUPE["Needed support, independent<br/>shared FastAPI/auth setup dedupe"]:::active
    T3["P0 behavior, independent<br/>trajectory search and lookup"]:::active
    PR["P0 behavior, independent<br/>proxy rollout survivability"]:::active
    PROFILES["Deferred decision<br/>capability profiles<br/>Rai design confirmation required"]:::future
    ACCESS["Deferred design<br/>delegated vs brokered external access<br/>grants and revocation"]:::future
    LIVE_CLEAN["Deferred cleanup<br/>executor heartbeat identity/<br/>row retention"]:::future

    BB["Deferred decision<br/>BuildBuddy hosted-run credential boundary"]:::future
    ING["Deferred support<br/>Event & Notification Hub<br/>external events -> Agent/Thread ingress"]:::future
    DT["Deferred<br/>driver-provided declarations/background control"]:::future
    AG["Deferred<br/>durable Agent identity + cross-agent read policy"]:::future
    PROD["Milestone<br/>production-capable governed action execution"]:::milestone

    AS --> MCP0
    EW --> MCP0
    DEL --> MCP0
    MCP0 --> MCPACCEPT
    MCP0 --> MCPAUTH
    CRED --> MCPAUTH
    MCPAUTH --> PROD
    DEL -. later Thread delivery .-> ING
    DEL --> APPROVALUI
    DEL --> MCPAGG
    EID --> MCPAGG
    EW --> HOSTEXEC
    MCPAGG -. replacement surface .-> RETIRE_TOOLS
    APPROVALUI -. replacement surface .-> RETIRE_TOOLS
    EID -. external identity .-> RETIRE_AGENT
    AG -. durable Agent/Thread model .-> RETIRE_AGENT

    DEDUPE -. independent support .-> PROD
    T3 -. independent product work .-> PROD
    PR -. independent reliability .-> PROD

    AS --> DT
    EW --> DT
    MCP0 --> AG
    AG --> EID
    EW -. retention cleanup .-> LIVE_CLEAN
```

The first executable Action/MCP path is `AS + EW + DEL -> MCP0 -> MCPACCEPT`. It uses a
credentialless, staging-owned deterministic streamable-HTTP MCP fixture and a real Claude/Codex
acceptance turn; it does not wait for GitHub OAuth. The later credentialed path is `MCP0 -> MCPAUTH ->
PROD`. Shared FastAPI/auth deduplication, trajectory search, and proxy survivability can proceed
without waiting for those gates. Their independence must not be described as evidence that the
current echo-only Action Service can execute production work.

The external-surface and migration tracks are intentionally separate from `MCP0`: `DEL` plus an
external static Agent identity are prerequisites for the Agentplane MCP aggregator, while the
integration-app approval UI consumes the same Action-state/Decision surface. Hostexec is another
Executor adapter behind `EW`; its final ordering relative to the credentialed MCP path is deferred.
Haku Console migration is split: Agent/conversation management and tool-call/approval management
can retire on different schedules after their respective replacement surfaces exist. Neither is a
prerequisite for the first Action/MCP acceptance.

## Named gates and acceptance evidence

### `AS` — Action schema contract

**P0 behavior:** a caller can submit one stable, reviewable, namespaced Action whose parameters are
validated before a Decision or dispatch, and whose result/error can be safely replayed.

**Needed support / decisions:**

1. Define **Action** as the code-owned capability concept and **ActionRequest** as one immutable
   invocation of that Action. Keep the existing request lifecycle and one-logical-intent model.
2. Choose a stable group/action name and catalog evolution behavior. No public `action_version` is
   required; execution re-checks the current executor/tool schema and refuses incompatible arguments.
3. Define parameter representation and validation. **Recommendation:** use the live MCP/tool schema
   for the first adapter; introduce a smaller typed contract only if a non-MCP executor needs it.
4. Define the result and stable error envelope, including which backend/provider details are safe to
   persist and return.
5. Define redaction and projection rules for inputs, results, errors, Decision views, events, logs,
   and replay fixtures. Do not add a generic `sensitivity` field to every Action.
6. Keep ActionGroup-to-executor and MCP-server/tool bindings in reviewed runtime configuration such
   as YAML, so backend/account changes do not require an image roll.

**Acceptance evidence for the first slice:** connect a small credentialless remote MCP fixture,
mirror its catalog, auto-allow one deterministic read-only Action, and prove with the deployed live
acceptance suite that a real Claude/Codex Agent can invoke it and receive a safe result. Include
negative tests for unknown group/action, malformed parameters, incompatible current tool schema,
malformed result/error, and sensitive data appearing in any projection or log. GitHub account access
is a separate later credentialed milestone below.

### `EW` — Executor wiring contract

**P0 behavior:** one accepted and allowed ActionRequest selects exactly one healthy configured
Executor, crosses a defined credential boundary, and produces one durable result or explicit unknown
outcome without replay.

**Needed support / decisions:**

1. Define how stable group/action identity selects an Executor and how capability/definition
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
   worker proves which request it is completing. **Landed in part:** the lease-token bearer a
   worker presents to heartbeat or complete is decided (`docs/executor_liveness.md`); the transport
   that would carry it out of process is not.
6. **Landed:** preserve the exactly-one claim — one Execution row, atomic claim before dispatch, no
   retry after dispatch may have begun, bounded lease expiry to `execution_unknown` on ambiguous
   loss, and adapter-agnostic reconciliation (late completion or an authoritative status lookup)
   restricted to an Execution already `execution_unknown`, never preempting a live attempt. See
   `docs/executor_liveness.md`.
7. Define idempotency-key behavior at request admission and at the backend boundary. A backend key
   may reduce duplicate effects but does not weaken the service's no-retry rule.
8. Define executor health and capability discovery as startup/readiness evidence, not a broad dynamic
   registry. **Landed in part:** an executor-level health heartbeat exists internally and feeds
   orphan-reason attribution; no external readiness/discovery endpoint exists yet.
9. **Landed in part by PR #5753:** `McpActionGroupExecutor` is a tested in-process adapter for one
   configured stdio MCP server. It mirrors `tools/list`, refreshes on notification or interval,
   rechecks the live tool schema before dispatch, initiates `tools/call`, and maps safe success,
   tool-error, and ambiguous transport outcomes. This does not yet wire the adapter into the
   production composition or provide a remote streamable-HTTP transport.
10. Wire the MCP adapter into the Action Service composition and reviewed runtime configuration,
    then select the credentialless remote MCP fixture and write the deployed acceptance test before
    calling `EW` complete. The fixture must expose one deterministic read-only tool and require no
    OAuth or provider credential.
11. Minimum evidence for that fixture: the named Action validates, allow auto-dispatches once, the
    MCP server receives the exact intended `tools/call`, duplicate Decision/start paths do not call it
    twice, success and safe failure are delivered, and ambiguous transport loss becomes unknown
    without retry. The test must live in `x/agentplane/acceptance/` and run against staging with a
    real LLM Agent, not remain a manual one-off.

`agentplane:v0.echo` remains explicitly fixture-only and cannot satisfy this gate.

### `MCP0` — credentialless remote MCP vertical slice

**P0 behavior:** a real staging Claude/Codex Agent discovers one configured ActionGroup, submits one
read-only ActionRequest, and polls durable Action events to a safe result produced by a remote MCP
server without the Agent or Action Service holding a provider credential.

**Needed support:** production composition for the landed MCP executor, a staging-owned deterministic
streamable-HTTP MCP fixture (or a deliberate transport extension from the current stdio adapter),
reviewed runtime binding, a narrow auto-allow policy for the fixture Action, and an acceptance
scenario in `x/agentplane/acceptance/test_action_mcp.py`.
Keep `EchoExecutor` as a unit-test fixture while it proves the coordinator seam; remove it from the
production composition only after the real adapter is wired and its replacement evidence passes.

**Acceptance evidence:** `//x/agentplane/acceptance:all` runs the scenario against the deployed
stack for both real harness providers, verifies catalog discovery, exactly one Action execution,
cursor-based event polling, and the exact safe tool result. It must not assert success from the
Agent's prose alone.

### `MCPAUTH` — credentialed MCP account and OAuth boundary

**Deferred support:** connect a user's GitHub MCP account without moving browser OAuth state or
refresh credentials into the harness. Haku Console or a shared credential broker should own the
operator identity, authorization-code + PKCE flow, callback state, token exchange/refresh, and
durable token association. Action Service should receive only an opaque account/credential binding
and own MCP discovery/call translation. If standalone operation later requires Action Service to own
OAuth, implement the smallest separately tested subset rather than copying Haku Console wholesale.
The static credential and binding model is a separate design decision below and requires Rai's
confirmation before implementation begins.

**Acceptance evidence:** a separate credentialed live scenario proves account linkage, catalog
refresh, one safe GitHub read, token refresh/reconnect, and negative isolation for an unbound or
different account. This milestone must not block `MCP0` or be folded into the credentialless fixture
test.

### `EID` — external Agent identity and authentication

**Deferred support:** authenticate external Agent clients, including Claude Code Web or another
non-Agentplane-hosted harness, as a known durable/static Agent identity distinct from any Thread or
Sandbox. The identity must be trusted by Agentplane before an external MCP client can use the
aggregator or receive approval state. Username, Thread ID, and caller-supplied provenance are not
identity authority.

**Acceptance evidence:** an external client authenticates as one configured Agent, cannot impersonate
another configured Agent, and remains distinct from the originating Thread/Sandbox model used by
hosted Agentplane workloads.

### `CRED` — static credential and binding design

**Deferred decision — Rai confirmation required:** define what a static credential is bound to
(Agent, external account, MCP server, or another authority), which component owns issuance and
storage, how expiry/refresh/revocation works, how a binding is selected at execution time, and what
the Agent/API may observe. This node is a design discussion, not an implementation task; do not
start code or schema work from it until Rai confirms the design.

### `PROFILES` — cross-cutting capability profiles

**Deferred decision — Rai confirmation required:** define a durable authority for capabilities shared by egress, approvals, MCP
reachability, and other tool permissions. Do not widen the landed launch-preset slice or store this
profile in Kubernetes merely to reserve the concept; the profile owner, inheritance, and policy
read/verification boundary remain open. Do not start implementation before the design is confirmed.

**Acceptance evidence:** one profile can be resolved consistently by each participating authority,
with explicit precedence and negative tests for stale, cross-Agent, or caller-supplied profile names.

### `ACCESS` — delegated versus brokered external access

**Deferred design:** choose per-system whether an Action uses the Agent's delegated identity, a
brokered operator credential, or a hybrid. Keep target-side RBAC and egress enforcement authoritative;
use grants/revocation reconciliation where a broker mints delegated authority. This is the broader
external-access policy behind `MCPAUTH` and `HOSTEXEC`, not a prerequisite for `MCP0`.

**Acceptance evidence:** a selected system proves the credential boundary, approval behavior, and
revocation/expiry semantics without putting a reusable privileged credential in the harness.

### `MCPAGG` — Agentplane MCP aggregator

**Deferred support:** replace Haku Console's MCP aggregator with an Agentplane-owned MCP surface so
MCP clients and harnesses running outside Agentplane's hosted Sandboxes can use the same approved
tool/action compatibility surface. It must authenticate the external Agent identity, route approval
requests through the canonical DecisionProvider, and expose no alternate lifecycle or authority
store.

**Dependencies:** `EID` for the caller principal and `DEL` for pending-approval notification and
decision delivery. The exact transport, tool projection, and migration order remain open.

### `HOSTEXEC` — hostexec-backed Action execution

**Deferred support:** add hostexec as an Action Service Executor adapter, preserving hostexec's
existing machine/user authorization, credential exchange, process-state, output, and no-retry
boundaries. This is an adapter behind `EW`, not a reason to build a generic worker framework first.

**Acceptance evidence:** one approved host command produces one durable Execution with bounded output
and safe terminal/unknown handling; duplicate starts do not run the command twice, and the Action
Service never receives a reusable host credential.

### `LIVE_CLEAN` — executor heartbeat retention cleanup

**Deferred cleanup:** executor liveness currently creates one heartbeat identity row per coordinator
process lifetime. Once deployment scale makes that accumulation meaningful, choose a stable executor
identity or bounded expiry/compaction policy and add retention tests; do not change the exactly-one
claim or unknown-outcome semantics while doing so.

### `APPROVALUI` — integration-app approval surface

**Deferred integration:** have the Agentplane integration app display pending Action approval requests
and submit allow/deny decisions through the Action Service's canonical operator API, as Haku Console
does today. It is a client/presentation layer, not a second Decision authority or Action state store.

**Dependencies:** the durable Action event/query and human Decision-provider notification pieces of
`DEL`; its UI may be delivered before or alongside `MCPAGG`.

### `RETIRE_AGENT` — Haku Console Agent/conversation management migration

**Deferred migration:** retire Haku Console's own Agent and conversation management only after
Agentplane has the external identity, durable Agent/Thread lifecycle, conversation read/control, and
replacement runtime surfaces required by Haku. This is a migration and decommissioning milestone,
not a prerequisite for Action execution; preserve explicit read/export and rollback evidence before
removing the old owner.

### `RETIRE_TOOLS` — Haku Console tool-call and approval management migration

**Deferred migration:** retire Haku Console's connected-MCP catalog, tool-call application/approval
queue, and related tool-call management only after the Agentplane MCP aggregator, integration-app
approval UI, credential bindings, and canonical Action/Decision APIs cover the required workflows.
This track may move independently of Agent/conversation management: Haku Console may continue to own
conversations while Agentplane owns external tool calls, or the reverse during a staged migration.
Preserve tool-call audit/export and rollback evidence before removing the old owner.

### `DEL` — decision and Action-state contract

**P0 behavior:** submission remains non-blocking; the Action API and durable Action events expose a pending
human Decision and the eventual Decision/Execution result with bounded provider-authored reason
evidence. Originating-Agent/Thread notification is a later integration node, not a prerequisite for
proving that an Agent can use an Action backed by MCP.

**Observed evidence — synchronous DecisionProvider aggregation landed.** PR
[#5732](https://github.com/agentydragon/ducktape/pull/5732) added deny-dominant aggregation of
configured synchronous non-human providers ahead of the existing human path, with bounded
provider-authored reason evidence and a shared optimistic-version/idempotency commit path for both
human and auto-provider Decisions. See [`async_approvals.md`](async_approvals.md).

**Needed support:** human decision callbacks, withdrawal before execution, bounded progress, redacted
payload projection, and what an Agent receives for `execution_unknown`. Durable Action event
append/query with cursor-based (`after_sequence`) polling is landed; see `action_service/README.md`.
A separate outbox is not required for this slice, and the never-drained `action_outbox` table has
been dropped.

**Acceptance evidence:** a scripted replay covering submit -> pending -> allow/deny -> one execution
or no execution -> Action API polling, including process restart and duplicate callback delivery.
Landed: restart-surviving event sequence, cursor/pagination polling, and redaction of
arguments/credentials/exceptions/private operator reason while surfacing the bounded provider
reason and safe terminal result. Open: human-provider notification and withdrawal evidence.

### `ING` — Event & Notification Hub

**Deferred support:** consume Action events and external sources such as GitHub/Calendar, match
user/Agent subscriptions, and deliver structured events into an Agent/Thread ingress. The Hub owns
subscription matching, deduplication, batching/debounce, rate limits, backpressure, offline delivery,
and Thread wake/queue semantics. It is not an executor or an Action decision authority.

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
- cross-cutting capability profiles — see [`profiles.md`](profiles.md);
- delegated-versus-brokered external-access policy and grant/revocation semantics — see
  [`external_access.md`](external_access.md);
- MCP registry, dynamic action marketplace, standing grants, and cross-agent permissions;
- production executor implementation in this planning PR;
- per-destination workload audiences until recipient isolation is required;
- broad profiles beyond the landed launch-preset slice; and
- cryptographic Decision signing until Decisions cross a boundary that requires it.
