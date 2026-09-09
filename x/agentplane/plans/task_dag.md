# Agentplane task DAG

This is the authoritative project overview for Agentplane. It records landed behavior as evidence and
keeps only work with a current user-visible outcome or a named design gate in the active path. Edges
are technical dependencies; packages without an edge can proceed independently. Operator priority
is recorded separately below and does not add dependency edges. Labels mean:

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
`ActionRequest`, `Decision`, `Execution`, and state events. The unused outbox has since been dropped. The
current caller envelope accepts exactly a 1–200 character `idempotency_key`, a structured
`action` object with separate `group` and `name` fields, a JSON-object `arguments`, and optional JSON-object `origin`/`correlation`; extra
top-level fields are rejected, and origin/correlation are untrusted provenance. Workload callers can read their own redacted records; the operator surface can read all
and issue an expected-version, idempotent human allow/deny Decision. Allow auto-dispatches exactly
one Execution; there are no blind retries, and an ambiguous outcome becomes `execution_unknown`
through the bounded lease/recovery contract in [`../docs/executor_liveness.md`](../docs/executor_liveness.md).

The canonical service now composes reviewed MCP groups through `McpActionGroupExecutor`,
with stdio and credentialless streamable HTTP, initial discovery, schema rechecks, and
lifecycle-owned cleanup. Real MCP fixtures exercise dispatch; isolated counting/failure
executors remain for concurrency and recovery tests. There is no dedicated Echo executor.

**Observed evidence — remote MCP and operator review implementation.**
[#5886](https://github.com/agentydragon/ducktape/pull/5886) hardens the existing streamable-HTTP
runtime and tests rendered staging configuration against the existing Everything Service. Production
composition, discovery/schema checks, and the bounded fixture policy are implemented, not open tasks.
[#5820](https://github.com/agentydragon/ducktape/pull/5820) implements PostgreSQL browser sessions
and request-bound operator federation; [#5827](https://github.com/agentydragon/ducktape/pull/5827)
adds Authentik configuration. [#5876](https://github.com/agentydragon/ducktape/pull/5876) exposes
canonical events through the BFF; [#5881](https://github.com/agentydragon/ducktape/pull/5881) shares
one human `decision_note` with caller and operator.

**Needed support — live verification only.** Run `//x/agentplane/acceptance:test_mcp` with the
real harnesses, Everything deployment, dedicated acceptance operator, and normal OIDC/BFF path.
The bootstrap and scenarios are implemented; rollout, real Authentik claims, network reachability,
and live Agent results remain unverified here. Do not reimplement those components or treat CI as
that deployed proof. See [acceptance](../acceptance/README.md) and
[operator federation](../docs/operator_federation.md). Workload identities never gain operator authority.

**Observed evidence — launch presets landed.** PR
[#5648](https://github.com/agentydragon/ducktape/pull/5648) landed the app-owned `SandboxPreset` and
`ThreadPreset` first slice, including `public-coder`, runner initialization, UI selection, and the
manual live acceptance target. Broader capability profiles remain deferred; see
[`../docs/launch_presets.md`](../docs/launch_presets.md) and [`profiles.md`](profiles.md).

**Observed evidence — executor liveness and orphan recovery.** Executor health heartbeats,
per-Execution leases, bounded expiry, and authenticated reconciliation of `execution_unknown`
are implemented. In-process MCP composition is landed; a future out-of-process adapter must
justify and prove its own dispatch transport rather than reopening the existing claim contract.
See [executor liveness](../docs/executor_liveness.md).

**Observed evidence — egress rules API boundary landed.** PR
[#5701](https://github.com/agentydragon/ducktape/pull/5701) made
`http://agentplane-egress.agentplane-staging.svc.cluster.local/v1/rules` an ordinary destination:
normal policy and exact workload-placeholder substitution, then independent destination bearer
validation through `SandboxPrincipalAuthenticator`. Service port 80 targets a separate API listener
in the same process/Pod; port 8888 remains the forward proxy. `RulesProjection` shares the enforcement
index and the redacted response contract. No local-dispatch branch or new credential mode is needed.

## Operator priority

Prioritize working deployed Claude.ai access to the Action Service MCP facade (`CLAUDEAI`), then
transcript search/lookup (`T3`). Search is technically independent; this is the desired order of
work, not a claim that its implementation depends on MCP. Additional local Claude Code acceptance
in `EXTERNALMCP` and full Haku migration are not part of that priority condition.

## DAG

```mermaid
flowchart TB
    classDef active fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a,stroke-width:3px
    classDef decision fill:#ffedd5,stroke:#c2410c,color:#7c2d12,stroke-width:2px,stroke-dasharray:5 3
    classDef future fill:#f3f4f6,stroke:#6b7280,color:#374151
    classDef milestone fill:#ede9fe,stroke:#6d28d9,color:#4c1d95,stroke-width:2px

    AS["Observed evidence<br/>Action schema/catalog and projections"]:::milestone
    EW["Observed evidence<br/>MCP runtime/config, claim and liveness<br/>CI proof; live verification in MCP0"]:::milestone
    DEL["Observed evidence<br/>Decision aggregation, shared note,<br/>canonical event/query API"]:::milestone
    CANCEL["Ready behavior<br/>owning caller cancels before dispatch claim<br/>no expected-version requirement"]:::active
    DEL --> CANCEL
    MCP0["P0 behavior<br/>credentialless remote MCP Action<br/>real staging LLM acceptance"]:::active
    MCPAUTH["Deferred support<br/>credentialed MCP account<br/>OAuth + credential-broker boundary"]:::future
    CRED["Deferred decision<br/>static credential + binding design<br/>ownership, lifecycle, revocation"]:::future
    MCPACCEPT["Milestone<br/>rerunnable Action/MCP acceptance<br/>against the deployed stack"]:::milestone
    EID["Planned support<br/>configured static external identity<br/>trusted caller + connection binding"]:::future
    POLICYBIND["Design gate<br/>shared ActionPolicySets + bindings<br/>model, storage, ownership"]:::decision
    MCPOAUTH["Planned support<br/>OAuth/DCR + integration-app enrollment<br/>named Connections, rename/unbind/rebind"]:::future
    CALLERPOLICY["Planned support<br/>configured caller Action bounds<br/>and auto-approval deciders"]:::future
    WID["Observed evidence<br/>SandboxPrincipal<br/>workload authentication"]:::milestone
    SBPOLICY["Planned behavior<br/>auto-approve configured Actions<br/>from trusted Sandbox types"]:::future
    MCPFRONT["Planned support<br/>generic Action MCP tools<br/>discover, request, read receipts/events"]:::future
    CLAUDEAI["Priority milestone<br/>working Claude.ai MCP facade<br/>deployed Action execution"]:::active
    EXTERNALMCP["Planned milestone<br/>Claude.ai + external Claude Code<br/>identity-bound Action execution"]:::future
    MCPAGG["Deferred migration<br/>replace Haku Console MCP aggregator<br/>external harness/client compatibility"]:::future
    HOSTEXEC["Deferred adapter<br/>hostexec-backed Action execution"]:::future
    APPROVALUI["Needed live evidence<br/>deployed operator federation + BFF<br/>identity and approval proof"]:::active
    RETIRE_AGENT["Deferred migration<br/>retire Haku Console Agent/<br/>conversation management"]:::future
    RETIRE_TOOLS["Deferred migration<br/>retire Haku Console tool-call/<br/>approval management"]:::future
    INPUT_DELIVERY["P0 behavior, independent<br/>input delivery/replay semantics<br/>provider research and captures first"]:::active
    T3["Lower priority<br/>trajectory search and lookup"]:::future
    PR["P0 behavior, independent<br/>proxy rollout survivability"]:::active
    PROFILES["Deferred decision<br/>capability profiles<br/>Rai design confirmation required"]:::future
    ACCESS["Deferred design<br/>delegated vs brokered external access<br/>grants and revocation"]:::future
    LIVE_CLEAN["Deferred cleanup<br/>executor heartbeat identity/<br/>row retention"]:::future

    BB["Deferred decision<br/>BuildBuddy hosted-run credential boundary"]:::future
    ING["Deferred support<br/>Event & Notification Hub<br/>external events -> Agent/Thread ingress"]:::future
    DT["Deferred<br/>driver-provided declarations/background control"]:::future
    AG["Deferred<br/>hosted Thread lifecycle<br/>cross-Identity read policy"]:::future
    PROD["Milestone<br/>production-capable governed action execution"]:::milestone

    AS --> MCP0
    EW --> MCP0
    DEL --> MCP0
    MCP0 --> MCPACCEPT
    MCP0 --> MCPAUTH
    CRED --> MCPAUTH
    MCPAUTH --> PROD
    DEL -. later Thread delivery .-> ING
    DEL --> MCPFRONT
    AS --> MCPFRONT
    WID --> MCPFRONT
    EID --> MCPOAUTH
    POLICYBIND --> EID
    POLICYBIND --> CALLERPOLICY
    DEL --> CALLERPOLICY
    AS --> CALLERPOLICY
    CALLERPOLICY --> SBPOLICY
    WID --> SBPOLICY
    MCPFRONT --> CLAUDEAI
    MCPOAUTH --> CLAUDEAI
    CALLERPOLICY --> CLAUDEAI
    EW --> CLAUDEAI
    APPROVALUI --> CLAUDEAI
    CLAUDEAI --> EXTERNALMCP
    EXTERNALMCP --> MCPAGG
    MCPFRONT -. replacement surface .-> MCPAGG
    MCPFRONT -. replacement surface .-> RETIRE_TOOLS
    EW --> HOSTEXEC
    MCPAGG -. replacement surface .-> RETIRE_TOOLS
    APPROVALUI -. replacement surface .-> RETIRE_TOOLS
    EID -. external identity .-> RETIRE_AGENT
    AG -. hosted Thread lifecycle .-> RETIRE_AGENT

    INPUT_DELIVERY -. reliable Thread ingress .-> ING
    T3 -. product work .-> PROD
    PR -. independent reliability .-> PROD

    AS --> DT
    EW --> DT
    MCP0 --> AG
    EW -. retention cleanup .-> LIVE_CLEAN
```

The first executable Action/MCP path is `AS + EW + DEL -> MCP0 -> MCPACCEPT`. It uses a
credentialless, staging-owned deterministic streamable-HTTP MCP fixture and a real Claude/Codex
acceptance turn; it does not wait for GitHub OAuth. The later credentialed path is `MCP0 -> MCPAUTH ->
PROD`. Input-delivery research, trajectory search, and proxy survivability are technically independent;
the operator priority above orders the work on search.
The AS/EW/DEL nodes record landed contracts, not implementation prerequisites still waiting to be built.
These independent tracks do not prove deployed Action execution. Shared app/auth/client/test setup
has no outstanding extraction justified by the current consumers; deduplication is not a scheduled
work item or a production-readiness prerequisite.

The external-client track is single-operator and independent of `MCP0` and the broader `AG` model.
Its product terms are Identity (configured authority), Connection (runtime named client enrollment), and Thread
(execution/conversation state); it adds no multi-operator management or per-operator ownership model.
`POLICYBIND` first settles policy-binding storage and the Identity/workload model, including whether
Sandbox types bind policies directly or resolve a configured Identity. The [Action policy plan](action_policies.md)
records app-configuration, Kubernetes, PostgreSQL, and mixed alternatives; no storage/schema choice is selected.
Its first Identity is configured and static (`EID`); OAuth/DCR enrollment (`MCPOAUTH`) binds a Connection to
it, and `CALLERPOLICY` supplies bounded per-Identity auto-approval. These join the canonical MCP frontend
(`MCPFRONT`), landed Executor support, and deployed operator review first at `CLAUDEAI`: a real
Claude.ai connection running governed Actions. `EXTERNALMCP` additionally proves independently
running Claude Code. The [external connection plan](external_mcp_connections.md)
owns the workflow and remaining design choices. Static identity does not select static credentials
or wait for `CRED`/`PROFILES`; outbound backend-account OAuth remains the separate `MCPAUTH` track.
`EXTERNALMCP` is the initial client proof for `MCPAGG`, not proof of full Haku tool parity.
Hosted harnesses continue to call Actions from their Sandbox Threads: `WID + CALLERPOLICY -> SBPOLICY`
adds configurable auto-approval by trusted Sandbox type. The [Action policy plan](action_policies.md)
owns the shared bounds and deciders. Its Sandbox slice is independent of `EID` and `MCPOAUTH`; a type
is a policy selector, not a shared caller identity or authenticated Thread.
Hostexec is another Executor adapter behind `EW`; its final ordering relative to the credentialed
MCP path is deferred.
Haku Console migration is split: Agent/conversation management and tool-call/approval management
can retire on different schedules after their respective replacement surfaces exist. Neither is a
prerequisite for the first Action/MCP acceptance.

## Named gates and acceptance evidence

### `AS` — Action schema contract

**P0 behavior:** a caller can submit one stable, reviewable, namespaced Action whose parameters are
validated before a Decision or dispatch, and whose result/error can be safely replayed.

**Observed evidence:** separate group/name identity, reviewed runtime bindings, discovered schemas,
admission and execution rechecks, and canonical safe result/error projections are implemented.
See [Action Service](../action_service/README.md). Remaining work is deployed MCP0 verification;
credentialed or non-MCP adapters must separately establish their boundaries.

### `EW` — Executor wiring contract

**Observed evidence:** the in-process MCP executor supports stdio and credentialless streamable HTTP.
Reviewed settings select groups/backends; invalid bindings or initial discovery abort startup.
The existing Everything Service is bound in staging settings. CI tests exercise production
composition with real HTTP MCP and PostgreSQL, and render Kustomize through the settings parser.
One Execution, atomic claim, no blind retry, lease expiry, and unknown-outcome reconciliation are
implemented. [#5886](https://github.com/agentydragon/ducktape/pull/5886) records the readiness tests.

**Needed support:** run the deployed MCP0 acceptance below. **Deferred:** out-of-process transport,
credentialed adapters, adapter-specific status lookup, and bounded progress for a real long-running
consumer. None requires another generic executor framework before that consumer exists.

### `MCP0` — credentialless remote MCP vertical slice

**P0 behavior:** a real staging Claude/Codex Agent discovers one configured ActionGroup, submits one
read-only ActionRequest, and polls durable Action events to a safe result produced by a remote MCP
server without the Agent or Action Service holding a provider credential.

**Observed evidence:** production composition, remote transport, staging Everything binding,
bounded echo auto-allow policy, and `x/agentplane/acceptance/test_mcp.py` are implemented.

**Needed support:** verify deployed images/configuration and run that suite through the real
OIDC/BFF and harness paths. Keep real MCP tools for success cases and isolated doubles only for
controlled failures/concurrency. CI composition tests do not satisfy this live gate.

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

### `EID` — configured external Identity and authentication

**Planned first slice:** a configured static external identity, independent of Thread, Sandbox,
OAuth client registration, and credential lifetime. It is the caller known to Action authorization,
DecisionProviders, and execution; an authorized connection binding preserves exact submission
provenance and revocation. Use Identity for this authority, Connection for the runtime named client
enrollment and its current binding, and Thread for execution/conversation state. The single operator configures Identities and authorizes
Connections. Multiple-operator management and per-operator ownership are out of scope, as are the
full `AG` lifecycle and static-bearer schemes. Username and caller-supplied provenance are not authority.

**Design gate:** resolve `POLICYBIND` for Identity references and policy associations, then
single-operator consent, Connection granularity, caller read/idempotency scope, and
revocation/dispatch consistency in the
[external connection plan](external_mcp_connections.md) before implementing the schema.

**Acceptance evidence:** a trusted connection resolves to identity A throughout an Action's
lifecycle; B cannot impersonate A or read its receipts. Refresh preserves identity, reconnect
requires authorized binding, and disabling A invalidates its bindings under the defined contract.
Preserve the exact authenticated OAuth client registration, Connection, and grant/binding revision
on each Action independently of its owning Identity. Two clients sharing one Identity remain
distinguishable in audit; rename/rebind/removal and duplicate submission cannot rewrite attribution.

### `MCPOAUTH` — OAuth/DCR enrollment and runtime Connection management

**Planned support:** Claude.ai and independently running harnesses such as Claude Code discover the
remote MCP endpoint and complete their OAuth flow, including DCR support, operator consent,
and token exchange. During enrollment the operator names the client Connection and selects an
existing configured `EID`. Connections are persisted at runtime and can be renamed, unbound, or
rebound to another configured Identity. DCR metadata alone grants no Action authority. Keep this inbound
connection separate from `MCPAUTH`, which connects Executors to credentialed upstream accounts.

**UI ownership:** enrollment consent/name/Identity selection and subsequent Connection management
live in the integration app. DCR registration is machine-to-machine; the authorization endpoint
hands the browser to that UI, the app authenticates the single operator and records consent through
its BFF, and the authorization server returns a code to the client's registered callback. Keep
operator-login credentials/state separate from the client's OAuth transaction and eventual token.

**Design gate / acceptance:** choose the authorization-server owner and reusable OAuth machinery,
including the transaction-bound integration-app handoff and authenticated BFF completion/management
API. Inspect the existing operator federation for reuse; its current Action-review support does not
implement this enrollment protocol. Then prove the real browser flow, naming/Identity selection, resource-bound tokens, refresh,
reconnect, rename, unbind, and rebind. Settle binding history, pending Actions/old receipt scope, and
whether rebind changes existing tokens' effective authority or requires reauthorization. Registration
mechanism and mutable names must not determine durable identity. See the
[external connection plan](external_mcp_connections.md), including DCR/CIMD compatibility.

### `POLICYBIND` — policy-binding model and storage

**Open design decision:** identify where policy definitions, Identity-to-policy bindings, and
Sandbox-type/instance associations live and how they are modeled. Distinguish configured policy
bindings from runtime named Connection-to-Identity bindings created/edited during enrollment and
management. Choose direct workload policy selection versus workload-to-Identity mapping,
cardinality/precedence, stable IDs, mutation ownership, and rollout/revocation semantics. This is
single-operator; multiple-operator management is out of scope.

**Required reuse:** a `public-coder` Sandbox type and a distinct external `claude-wyrm2` Identity can
reference the same canonical ActionPolicySet (working name). One edit changes their Action policy
under the chosen consistency contract without copying rules into each caller's configuration.
Explicit references share permissions, not caller identity, receipt ownership, or upstream credentials.
The first slice does not require a general inheritance system; the [Action policy plan](action_policies.md)
owns the worked example, composition choices, and shared-policy acceptance.

The [Action policy plan](action_policies.md) compares app configuration, Kubernetes resources,
PostgreSQL, and mixed ownership. No option is selected. For Kubernetes, define resource shape,
RBAC, references, and informer freshness. Runtime Connections need a writable authority, whether
PostgreSQL or service-owned Kubernetes resources, even with Git-managed policy definitions.
Walk one external and one hosted request through enrollment/rebind, resolution, policy edit, and dispatch
before implementing `EID` or `CALLERPOLICY` persistence. Existing Sandbox authentication and Action
execution remain usable while this design is open.

### `CALLERPOLICY` — bounded Action deciders for external and hosted callers

**Planned support:** configure exact Actions/argument conditions and auto-approval deciders selected
through reusable policy-set references from an external Identity or an authenticated Sandbox's
configured type. Reuse `DEL` aggregation and
the canonical Decision/Execution lifecycle. Mandatory authorization bounds must fail closed even
when another provider allows; permitted requests without auto-approval may take the human path.

**Design gate / acceptance:** after `POLICYBIND`, the [Action policy plan](action_policies.md) owns typed caller
selectors, mandatory bounds, decider composition, and policy changes between submission and dispatch.
Prove matching auto-allow, changed-argument review/deny, caller-class isolation, and rejection on
failed mandatory bounds. External selector integration needs `EID`; the shared machinery and Sandbox
slice do not wait for it. Broad `PROFILES` and a policy DSL remain deferred.

### `SBPOLICY` — configured auto-approval for Sandbox types

**Planned behavior:** an agent harness running in a Thread in a Sandbox calls the Action Service
through its existing workload authentication. Configured Actions can auto-approve for configured
Sandbox types under bounded conditions, without introducing an OAuth Connection for that harness.
`WID` records the landed `SandboxPrincipal` authentication; trusted type classification is new work.

**Design gate / acceptance:** choose the authority for type (preset, template association, or another
configured category), who may assign/change it, and classification lifetime. Verify real workload
submissions with matching/different types and arguments, forged or stale classifications, and policy
changes before dispatch. Same-type Sandboxes retain separate caller reads/idempotency; Threads within
one Sandbox retain the current shared workload scope. See [Action policies](action_policies.md).

### `CLAUDEAI` — working Claude.ai MCP facade before transcript search

**Operator-priority milestone:** the operator can connect Claude.ai to the deployed Action Service
MCP facade, name/bind the Connection through integration-app enrollment, discover Actions, and use
them under the configured Identity/policy with real results. Prove the bounded fixture auto-approval
and human-review/result paths against canonical Action records through the actual Claude.ai client.
Registration alone, mocks, and CI composition tests do not establish this user-visible outcome.

The broader `EXTERNALMCP` milestone also covers local Claude Code. The operator priority above names
this Claude.ai outcome specifically; it does not require the additional client or full Haku migration.

### `T3` — trajectory search and lookup

**Lower-priority product work:** search and look up stored trajectories. This is technically
independent of `CLAUDEAI`; prioritize the working facade first. Existing transcript persistence and
unrelated lifecycle reliability work are not reclassified as search implementation by this ordering.

### `EXTERNALMCP` — hosted clients and external harnesses using governed Actions

**Planned milestone:** `EID`/`MCPOAUTH`, `CALLERPOLICY`, and `MCPFRONT` compose with the existing Executor
and operator-review paths. Real Claude.ai and local Claude Code connections (for example on wyrm2)
use selected static Identities to discover and run one credentialless Action under bounded
auto-approval, or receive a durable pending receipt
and read the result after human review. Independently verify Action ownership, binding, Decisions,
Execution, replay, isolation, and revocation as specified in the
[external connection plan](external_mcp_connections.md). This milestone precedes Haku migration and
does not require backend account OAuth or the full Agent/conversation model. Verify each client's
OAuth/redirect/refresh/reconnect and approval/result flows. External harnesses need no Agentplane
Sandbox/Thread or upstream credentials; only Actions routed through this service are governed by it.

### `CRED` — static credential and binding design

This gate concerns static credentials and backend bindings; the configured static identity in
`EID` authenticates through OAuth in `MCPOAUTH` and does not depend on selecting a static credential.

**Deferred decision — Rai confirmation required:** define what a static credential is bound to
(Identity, external account, MCP server, or another authority), which component owns issuance and
storage, how expiry/refresh/revocation works, how a binding is selected at execution time, and what
the Agent/API may observe. This node is a design discussion, not an implementation task; do not
start code or schema work from it until Rai confirms the design.

### `PROFILES` — cross-cutting capability profiles

The Action-only reusable policy-set slice is planned under `POLICYBIND`/`CALLERPOLICY`; it can serve
both Sandbox types and external Identities without waiting for this broader profile.

**Deferred decision — Rai confirmation required:** define a durable authority for capabilities shared by egress, approvals, MCP
reachability, and other tool permissions. Do not widen the landed launch-preset slice merely to
reserve the concept; Kubernetes remains a storage candidate, and the profile owner, inheritance, and policy
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

### `MCPFRONT` — Action Service MCP frontend

**Planned support — smallest user-visible behavior:** an authenticated MCP client discovers
Actions, submits one ActionRequest with an optional bounded wait, and reads its pending Decision and
eventual safe result. This is external MCP presentation over the canonical Action API, not another
executor, remote MCP runtime, or lifecycle store.

**Placement selected:** host the MCP server in the Action Service process, sharing its catalog,
authentication, service/store, and lifecycle. No separate pod or sidecar is required for the first
slice. OAuth authorization-server placement remains a separate `MCPOAUTH` design question.

**Initial surface chosen:** fixed generic tools such as `list_actions`, `get_action`, and
`request_action`, plus request-status/event reads. An Action definition and a submitted ActionRequest
are distinct. Do not mirror Actions into individually exposed MCP tools in this first slice; the
[connection plan](external_mcp_connections.md) owns the working inventory, schemas to settle, and
client workflow. Detailed schemas/descriptions are opt-in through `include_fields`; default discovery
stays compact. Per-Action projection is an optional experiment contingent on evidence from the generic
interface, not a scheduled follow-on or required migration step.

**Dependencies:** `AS` for the canonical catalog, `DEL` for Decision/Action-state and durable events,
and observed `WID` for the Sandbox bearer path. The shared frontend accepts both Sandbox workload
bearers and external OAuth access tokens; `EID`/`MCPOAUTH` add the external path and are required for
`CLAUDEAI`, not for implementing the common tools or Sandbox-authenticated MCP. Caller-supplied Agent/Thread names and
origin/correlation are not identity authority. `MCPFRONT` is not a prerequisite for `MCP0`.
The initial clients are Claude.ai and external Claude Code through `MCPOAUTH`; `CALLERPOLICY` adds
bounded per-Identity deciders. Their combined acceptance is `EXTERNALMCP`. The [connection plan](external_mcp_connections.md)
keeps the remaining generic-tool schema and authorization design choices explicit.

**Needed support / contract:**

- **Authentication:** reuse workload bearer validation and live Sandbox resolution, including the
  existing egress token-substitution path, alongside external Connection/Identity resolution. Preserve
  each caller kind's ownership and policy context; no DCR requirement for Sandboxes, no operator
  authority, and no permissive fallback between validators. Both paths expose the same generic tools.
- **Catalog:** compact list/get views preserve group/name identity; `include_fields` explicitly
  selects existing input schemas and full descriptions. Do not embed the catalog in the generic
  tools' own schemas or return omitted detail through nested fields. Defer output schemas and other
  new Action metadata until after a working frontend; they do not gate `MCPFRONT` or `CLAUDEAI`.
  Reject unsupported field selections clearly. Never expose executor
  bindings or credentials, create an MCP-owned registry, or dispatch directly to an upstream tool.
- **Submit:** forward the canonical ActionRequest envelope and caller-scoped idempotency key under
  the authenticated external Identity or Sandbox principal. Return the durable request ID and current state,
  including `decision_pending`, immediately by default. Submission and request-status reads both
  offer an explicit bounded wait for decision resolution or terminal execution; expiry returns the
  current receipt, not an Action failure. An MCP wait timeout/disconnect never cancels or retries the
  durable Action. Internally, await pushed update notifications over channels/subscriptions; no busy
  loop or periodic database/API polling. Avoid missed updates at subscription setup, support writers
  in other processes/replicas, and clean up waiters. The [connection plan](external_mcp_connections.md)
  specifies bounded-wait and notification semantics.
- **Get / events:** read only that caller's canonical request and ordered durable Action events.
  `after_sequence` is the last event sequence already received; return later events in order, use
  the last returned sequence for the next poll, and return an empty list when none are newer.
  Reconnect/restart resumes from the same request ID and cursor, without another submission or
  execution. Do not add a frontend-owned cursor, queue, or event log.
- **Approvals:** canonical DecisionProviders and the existing operator UI/BFF remain the decision
  path, including expected-version/idempotent allow/deny. MCP clients cannot acquire operator
  authority or bypass the canonical single-Execution/no-blind-retry lifecycle.
- **Isolation / projection:** preserve caller-own reads and canonical redaction of arguments,
  results, errors, and events. The human `decision_note` remains shared unchanged with caller and
  operator, not a private channel; non-human `reason_code`/`reason_description` remain separate.

**Acceptance evidence:** a real external MCP client, authenticated as Identity A, discovers a configured
Action, submits it, observes pending state, and resumes event polling after reconnect and service
restart. Allow through the canonical operator BFF produces exactly one Execution and the same safe
result as the Action API; deny produces none. Replay duplicate submission/Decision delivery and
prove Identity B cannot get or poll A's request, forged provenance cannot change ownership, and no
operator-only arguments, credential-bearing bindings, or unsafe executor payloads leak through MCP.
Exercise bounded polling on submission and reads, including pending deadlines, decision-only versus
execution completion, already-complete requests, and reconnect without duplicate execution.
Verify notification-driven wakeups, setup-race safety, channel-loss handling, and no periodic state
queries while idle.
Separately prove the same MCP workflow with a Sandbox workload bearer, without DCR, retaining
per-Sandbox receipt/idempotency isolation and rejecting invalid credentials. This frontend auth proof
does not require the new configurable `SBPOLICY` behavior.
This integration proof comes before any frontend-specific persistence or framework.

### `MCPAGG` — Haku Console MCP aggregator replacement

**Deferred migration:** use `MCPFRONT` as the replacement external MCP presentation surface for Haku
Console's aggregator. Verify the required external harness/client workflows against it before
retiring the old surface; do not build a second frontend, approval coordinator, or authority store.
`EXTERNALMCP` proves Claude.ai and independently running Claude Code with OAuth and configured Identities.
Inventory and migrate the remaining Haku tools, policies, and client workflows separately; backend
credential requirements remain adapter-specific. The initial facade uses generic Action tools;
per-Action projection may never be needed and is not required for migration. Actual generic-client
evidence determines whether to explore it. The migration order remains open.
Tool-call/approval management retirement remains the separate `RETIRE_TOOLS` milestone.

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

### `APPROVALUI` — verify the deployed integration-app operator approval path

**Observed evidence:** PostgreSQL sessions, request-bound federation, Authentik configuration,
dedicated acceptance-operator bootstrap, and canonical BFF review/events are implemented.

**Needed support:** verify actual deployment and provider claims, distinct authorized identities
and rejected identities, then execute the existing BFF approval acceptance. Signed mock integration
is CI evidence, not deployed Authentik proof. Do not add another approval coordinator or require
push notifications for the polling UI.

### `RETIRE_AGENT` — Haku Console Agent/conversation management migration

**Deferred migration:** retire Haku Console's own Agent and conversation management only after
Agentplane has the external Identity, durable Thread lifecycle, conversation read/control, and
replacement runtime surfaces required by Haku. This is a migration and decommissioning milestone,
not a prerequisite for Action execution; preserve explicit read/export and rollback evidence before
removing the old owner.

### `RETIRE_TOOLS` — Haku Console tool-call and approval management migration

**Deferred migration:** retire Haku Console's connected-MCP catalog, tool-call application/approval
queue, and related tool-call management only after `MCPFRONT`, the `MCPAGG` compatibility migration,
integration-app approval UI, credential bindings, and canonical Action/Decision APIs cover the
required workflows.
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

**Needed support:** human-provider notifications, pre-claim cancellation through `CANCEL` below,
and bounded progress for a concrete consumer. Human callbacks, safe projections, and the
`execution_unknown` API state are implemented. Durable Action event
append/query with cursor-based (`after_sequence`) polling is landed; see `action_service/README.md`.
A separate outbox is not required for this slice, and the never-drained `action_outbox` table has
been dropped.

**Acceptance evidence:** a scripted replay covering submit -> pending -> allow/deny -> one execution
or no execution -> Action API polling, including process restart and duplicate callback delivery.
Landed: restart-surviving event sequence, cursor/pagination polling, and redaction of
credential-shaped arguments/results/errors and unsafe exceptions while surfacing the unchanged
human `decision_note`, bounded non-human provider reason evidence, and safe terminal result.
Open: human-provider notification and withdrawal evidence.

### `CANCEL` — owning-caller cancellation before dispatch claim

**Ready for implementation; semantic gate resolved:** cancellation guarantees the request will not
execute. Only its authenticated owning caller may cancel; use the existing caller ownership scope
(Sandbox namespace/UID for workloads, not Thread or shared policy). Require no expected version.
The atomic dispatch claim is the cutoff, even before the executor physically begins its work.

| Current state                                 | Cancellation result                             |
| --------------------------------------------- | ----------------------------------------------- |
| `decision_pending`                            | Transition to `cancelled`.                      |
| `allowed`, Execution still `pending_dispatch` | Cancel the request and its unclaimed Execution. |
| `dispatching` or `running`                    | Refuse as too late; leave execution untouched.  |
| `cancelled`                                   | Idempotent success.                             |
| `succeeded`, `failed`, or `denied`            | Report already finished; leave unchanged.       |
| `execution_unknown`                           | Refuse; execution may have happened.            |

Cancellation, Decision commit, and dispatch claim must serialize atomically. If cancellation wins,
no queued task, late approval, duplicate submission, or restart can subsequently start execution.
An intervening approval alone does not prevent cancellation while the Execution remains unclaimed.
Retain any earlier Decision and record cancellation actor/time plus the canonical state event.
Submission with the original idempotency key returns the same cancelled request; a deliberate new
attempt requires a new submission key. Repeated cancellation is safe without a version precondition.

There is no in-progress cancellation propagation, executor stop request, process killing, or promise
to undo side effects. Disconnecting/cancelling an MCP wait is not Action cancellation. Publish the
canonical cancellation transition through the same update-notification path as other state changes
when notification-driven waits are present; this does not make the cancellation API depend on the
MCP frontend. Conversely, the initial MCP frontend can land without a cancellation tool.

**Acceptance:** own-caller authorization and other-caller rejection; the full state table; competing
allow/deny/cancel/claim operations; already-scheduled dispatch; restart and duplicate delivery;
original-submission-key replay; preserved Decision/audit and state-event visibility. Prove that a
successful cancellation prevents executor invocation, not merely that the API returned `cancelled`.

### Decision note — existing query/BFF projection

**Landed behavior:** one bounded human-authored `decision_note` is stored and returned unchanged to
requesting caller and operator through the existing Action API polling/BFF. The migration preserves
existing notes; there is no private human-note field. Non-human provider `reason_code` and
`reason_description` remain separate bounded outcome evidence. API/BFF tests cover exact note
visibility and duplicate/stale decisions; push notification and offline Thread wake remain in `ING`.

### `INPUT_DELIVERY` — native queue evidence before common-protocol changes

**P0 behavior:** an input crossing the app/runner boundary has an honest, correlated delivery
outcome after disconnect/reconnect, without silently losing it or blindly submitting it twice.
This work is independent of live Action/MCP staging acceptance and is not Action cancellation.

**Needed support — mandatory first step:** re-read the landed
[Claude queue research](../docs/claude_input_queue.md),
[Claude/Codex protocol notes](../docs/provider_protocols.md),
[native harness evidence](../docs/harness_evidence.md),
[Claude runtime contracts](../docs/claude_runtime_contracts.md), and
[current common protocol](../docs/common_protocol.md), then inspect the pinned drivers/tests.
Reconsider the common protocol's input semantics from this evidence rather than assuming the
bridge is the only queue or that one input equals one turn.

- Claude: UUID command handles, lifecycle receipts, capability negotiation, targeted withdrawal,
  interrupt receipts/queued survivors, and coalescing that can make cancellation batch-granular.
  Do not interpret an ambiguous `cancelled:false` as a guaranteed no-op or fabricate receipts.
- Codex: distinguish joined input in the active turn's in-memory pending list from the separate
  experimental durable `thread/queue/*` API. Examine queue promotion, dispatch/delete locking,
  interruption, and correlated user-message evidence; an RPC acknowledgement is not consumption
  or completion, and a queue-changed notification alone does not prove promotion.

**Observed evidence boundary:** some queue behavior comes from declarations/static analysis, not
capture-pinned tests. Refresh live probes/captures against both pinned binaries for the behavior
being adopted before changing the common protocol; then encode supported behavior in scripted
native-harness CI tests against the controlled model endpoint. Missing capabilities and blocked
experiments stay explicit, not normalized into success. This research may justify common-protocol
changes, but it does not preselect a queue facade, selective cancellation, or a new persistence layer.

**Acceptance evidence:** exercise disconnect before delivery, delivery before observed receipt,
reconnect/replay with the same `input_id`, and restart. Prove duplicate-ID handling at each actual
boundary rather than assuming native idempotency. Include Claude coalescing/interrupt/withdrawal
and Codex join-versus-durable-queue cases, preserving raw native evidence and provider differences.
No acknowledgement, retry, steering, cancellation, or completion may be invented by the runner.
Decide the narrow common contract only after these observations; keep unsupported operations native
or explicitly unavailable. **Deferred:** generic queue management and unproven per-input cancellation.

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
- Caller-own and operator-all reads remain the v0 access scope. Operator arguments are exact; caller arguments and executor results/errors retain recursive credential-shaped redaction.

## Deferred

- capability matrices or a broad Agent identity/privilege framework;
- cross-cutting capability profiles — see [`profiles.md`](profiles.md);
- delegated-versus-brokered external-access policy and grant/revocation semantics — see
  [`external_access.md`](external_access.md);
- MCP registry, dynamic action marketplace, standing grants, and cross-agent permissions;
- per-destination workload audiences until recipient isolation is required;
- broad profiles beyond the landed launch-preset slice; and
- cryptographic Decision signing until Decisions cross a boundary that requires it.
