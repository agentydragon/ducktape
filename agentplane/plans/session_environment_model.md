# Sessions & environments

> Working design and discussion record, 2026-11-02. This is a proposal, not an implemented
> contract. The address inventory's prior baseline was `devel` at `9b44c17e60`; the subscription
> entry was reconciled against current `devel` at `a7b398dfc9` after [#14103](https://github.com/agentydragon/ducktape/pull/14103) merged.
> The original investigation used the source revisions linked at the end. Discussion entries
> are historical: later decisions and corrections supersede earlier suggestions. The shorter
> [Session/runtime model brief](session_runtime_model.md) remains useful context; this document
> records the more recent decisions and unresolved details.

## Decision status and implementation readiness

The direction is substantially decided, but the authority and recovery details needed to ship a
new Session directory safely are not. The distinction is between a chosen product model and a
finished implementation contract.

**Chosen direction:** public, unscoped Session UUIDs distinct from harness-native IDs; one inherent
required environment for today's environment-hosted native Sessions; central Sessions may have
zero or more environment attachments; a Dispatcher PostgreSQL Session record establishes logical
existence without waiting for its owner or starting a harness; History remains separately stored;
app archive is not Session deletion; central Sessions persist indefinitely for now; and authorized unattended inbox notices
must be capable of waking an environment. Record all attachment transitions in History eventually;
this is a correctness requirement, though not the highest priority. Existing systems do not yet
implement all of this.

**Completed prerequisites:** the task DAG records mandatory Dispatcher PostgreSQL and queue-backed
admission; the [opening/recovery inventory](session_opening_recovery.md) maps existing ownership;
[#14026](https://github.com/agentydragon/ducktape/pull/14026) fences uncertain Runtime reservation
writes; [#14030](https://github.com/agentydragon/ducktape/pull/14030) requires canonical UUIDs for
new Dispatcher openings; and [#14031](https://github.com/agentydragon/ducktape/pull/14031) checks
the exact Sandbox UID for new message inboxes. These do not implement the general directory.

**Implemented:** opening reads use the globally unique Session ID through Dispatcher
[`GetSessionOpening`](../runner_gateway/dispatcher/session_dispatcher.proto), exposed to the browser
through the operator-authenticated app facade and used by test callers through
[`SessionDispatcherClient.get_session_opening`](../runner_gateway/dispatcher/testing_client.py).
The [address inventory](#session-address-inventory) records this completed migration. The
[Dispatcher outcome contract](../runner_gateway/dispatcher/SPEC.md#native-opening-outcomes-and-dependent-commands)
defines native rejection, unconfirmed owner outcomes and dependent-command fencing. Cleanup forgets
the Session in Dispatcher; UUID uniqueness across a deleted record is a caller obligation, not a
server-enforced lifetime guarantee. This does not complete the directory.
All Session-targeted commands now require a Dispatcher Session record, including privileged callers and
interrupts. Gateway permits direct Runtime Session creation and mutation only to the combined
Gateway/Dispatcher workload; its read-only diagnostics and separate Executor service have their own grants.
These checks do not by themselves prove the remaining deletion-outcome fence. Public commands now
address the Session by ID; Dispatcher resolves and fences its placement.

**Ready next:** a [native-first Dispatcher Session authority](task_dag.md#session_directory_native--dispatcher-authority-for-environment-hosted-sessions)
using existing authorization. Specify deletion and uncertain-outcome fencing as part of that slice;
do not add a second competing writable directory. The current opening table is not a general
Session directory.

**Still require design:** central Sessions with no environment or multiple attachments, the central
principal and its authority, moving or changing wake orchestration, and cross-store deletion and
History semantics. The current opening table is not a general Session directory, and the central
sampler currently requires an executor.

Decisions still needed include the Session/environment principal and privilege relationship; the
exact Dispatcher/backend creation, ownership and idempotency protocol; attachment authorization,
revision and History publication; deletion and History handoff; and which component wakes a suspended
environment under which authorization. In particular, an Agentplane principal and external identity
provisioning are directions to evaluate, not a decision to use any particular identity provider or
share a Kubernetes ServiceAccount. The related
[Agentplane principal decision](task_dag.md#agent_principal_model_design--agentplane-principals-and-external-identities)
covers identity provisioning and trust boundaries.

A Session has one globally unique public ID. A resolver finds its responsible owner; that owner locates the harness continuation. Environment bindings describe where work can run.

**Operator direction, 2026-11-04:** callers that want to interact with a Session should address the
Session itself, without being bound to an environment or having to provide one. The Session owner
or Dispatcher resolves any placement an operation needs. Environment lifecycle and execution remain
environment-scoped, and a native harness can still require a particular environment internally;
neither changes the caller's Session identity. Preserve authorization, retained provenance and
exact-incarnation fencing when resolving or using an attachment.

<a id="proposal"></a>

## Two relationships, one Session

Identify each Session by a globally unique UUID. Resolve that ID to the **Session owner** responsible for its commands and continuation. The owner tracks the selected harness and its durable state. Represent an environment-hosted native harness with exactly one required environment in its placement. A centrally hosted Session can have zero or more separately managed environment attachments.

### Who manages the Session

A logical component scope: the Environment Runtime within an exact environment, or a central sampler scope. It accepts commands and knows the Session’s harness and continuation reference; it is not the harness process itself.

### Where central work may happen

A named, optional relationship to an exact environment identity. A central Session may have zero or more; an attachment does not itself imply a live connection or grant authority.

### A harness constraint

A native harness placement names one required environment and a specific harness state store. The running process is replaceable; the environment is not a detachable attachment.

**Key distinction.** A central Session can continue talking without an environment; one tool call can fail without ending it. A native Claude Session can be retained while its environment is suspended, but its harness cannot run elsewhere merely because the API has a Session ID.

Use _Session owner_, _native harness placement_ and _central environment attachment_ as domain concepts; the chosen Dispatcher PostgreSQL direction does not by itself require new Kubernetes CRDs, COSI resources, or network services. A normalized read-only view may show both environment relationships, but their write APIs and semantics differ.

<a id="objects"></a>

## What exists, and what points where

**Terminology correction:** the earlier draft used “Session home” for several different things. Here, **Session owner** is the responsible component scope; **harness placement** identifies the backend and any required environment; **continuation reference** identifies the durable store and native Session; and a **running harness process** is replaceable. None of these is the public Session UUID.

```mermaid
flowchart LR
    S[Session] --> O[Session owner]
    S --> A[Optional central attachments]
    O --> C[Harness continuation store]
    O -. required native placement .-> E[Environment]
    A --> E
    E --> I[Replaceable environment incarnation]
```

The dashed arrow represents the native harness placement’s required environment. The attachment path is for centrally hosted Sessions; it is not a second writable copy of the native placement.

| Concept                    | What it is                                                                                                                                                                                              | Lifetime and ownership                                                                                                                                                             |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Session                    | Globally unique public UUID, its logical principal, selected harness backend, and lifecycle.                                                                                                            | Managed by its assigned Session owner. Its identity survives host-process restarts; continuation after data loss depends on retained harness state.                                |
| Session owner              | The logical component responsible for backend admission, execution and the mapping to a harness and its continuation; found by resolving the Session UUID. The Dispatcher owns the logical reservation. | The scope is an exact environment’s Runtime or a central sampler scope. It is not an individual process or a storage path.                                                         |
| Harness placement          | The selected environment and harness state store, including which kind of harness manages it. The Session owner also retains the native Session ID once known.                                          | Multiple Sessions can share a harness store and running app-server; a store is not a process. Store identity, path and process identity have different lifetimes.                  |
| Running harness            | A transient process: e.g. one Claude process, a shared Codex app-server, or the central sampler’s JavaScript host.                                                                                      | It can serve one or many Sessions. Process death does not itself delete the Session or prove work did not happen. Environment Runtime is the managing component, not this process. |
| Continuation store         | State required to resume the actual harness. The Session owner locates it through a durable store reference and native conversation ID, which may be exposed to authorized callers.                     | May contain several Sessions; its concurrency and recovery rules are backend-specific. A transcript alone is not a replacement.                                                    |
| Environment                | A logical execution target with a stable UID, workspace/resources and an execution contract.                                                                                                            | Pod/VM and Runtime instances can change while the same environment persists. Same name with a new UID is a new environment.                                                        |
| Central attachment         | A Session-local, named reference to an environment and requested capabilities, separately managed by a centrally hosted Session.                                                                        | Desired attachments can be persisted with the Session; availability and connections are observed state. Native placement is not duplicated as an attachment.                       |
| Execution session / handle | A backend connection, process ID, file handle or output cursor inside an environment.                                                                                                                   | Scoped to the relevant environment incarnation and executor lifetime. Never a durable Session address.                                                                             |
| History                    | Retained evidence and conversation projections, with attributed source events.                                                                                                                          | Separate from continuation. Retention and authorization after environment deletion need an explicit policy.                                                                        |

Current grounding: [durable state and running hosts](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/session_runtime_model.md#durable-state-and-running-hosts) · [Execution sessions](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/execution/SPEC.md#sessions-and-effects).

<a id="cases"></a>

## One model, different constraints

The relationships are the same vocabulary; the supported operations are not necessarily the same.

### Claude runs where it acts

**Owner and placement** Environment Runtime in exact environment `E1` manages the Session; the selected backend is Claude. Its continuation reference names the relevant Claude state and native Session ID when known.

**Environment** `E1` is an inherent part of native harness placement, not an attachment that can be detached or retargeted. Additional environments for native Sessions are not planned now.

**Runtime** One Claude process for this Session under the current adapter; startup is lazy. State needed to resume remains in the designated native store.

**If E1 goes away** During suspension the Session is unavailable but retained. After deletion, continuation requires independently retained/restorable native state; History alone is insufficient.

### Codex shares a host, not a Session

**Owner and placement** Two Sessions are managed by Environment Runtime in `E1` and have separate public IDs and continuation mappings.

**Host and store** One app-server and its Codex home can serve multiple threads. An app-server incarnation and a store are not public Session identities.

**Control** Interrupting a turn targets one Session and turn. Restarting the shared server affects its hosted Sessions without silently replaying any of their work.

**Environment** Each Session’s native placement names `E1`. Separate per-Session cwd and instructions do not make E1 an isolation boundary between them.

### A central Session can use zero, one, or many environments

**Owner and placement** A central sampler scope manages Session `S` and its continuation. Its public UUID remains addressable without any environment.

**Bindings** Initially none. Later `dev → E1` and `test → E2` are added; each tool invocation names its target.

**If E1 suspends or is deleted** `dev` becomes unavailable or gone; `test`, the central Session, and provider conversation remain. The agent can handle the failed operation.

**Important gap** Today's sampler binds one fixed Sandbox UID and requires an initial ready executor. Zero/multiple environments and post-deletion central dispatch are proposed work, not current capability.

Current behavior: [native Sessions](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/docs/session_protocol.md#sessions) · [central sampler contract](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/central_sampler/SPEC.md).

<a id="address"></a>

## Identify globally; resolve placement

Selected direction: callers name a Session by one globally unique, unscoped public UUID. A durable Session directory resolves it to the current Session owner and placement. Dispatcher PostgreSQL owns that directory and live Session records, separate from History’s database; the directory does not require a separate Deployment. Whether placement generations are needed and their precise representation remain implementation questions.

Every Session-targeted command requires a Dispatcher Session record, including commands from
privileged services and interrupts. The record establishes the Session to address; it is
not itself permission to send or wake. Dispatcher now enforces this for queued commands and interrupts,
and the public Gateway restricts Runtime Session mutations to the Gateway/Dispatcher workload.
All four Session commands, including interrupts, receive a durable Dispatch receipt.

For the first environment-hosted command cutover, keep the existing direct and delegated caller
checks and scoped service trust; wake remains separately authorized. When deletion races accepted
work, fail commands proven not to have left Dispatcher, report unresolved in-flight outcomes as
unknown, and never redirect work to a replacement environment. The deletion fence and the location
and lifetime of outcome evidence still need design; Dispatcher does not retain a deleted Session UUID.

Opening reads now target the Session ID through Dispatcher
[`GET /v1/sessions/{session_id}/opening`](../runner_gateway/dispatcher/session_dispatcher.proto).
The [browser adapter](../app/frontend/sandboxes/client.ts) calls that endpoint through the
operator-authenticated app facade; test callers use
[`SessionDispatcherClient.get_session_opening`](../runner_gateway/dispatcher/testing_client.py).
This lookup does not by itself implement the proposed Session directory.

```text
public address:  session_id = UUID

resolve(session_id) → {
  owner: environment_harness_host { environment_uid }
       | central_sampler          { scope_id },
  placement_generation,
  state: reserving | assigned | unavailable | retired
}

// Native Session record, maintained by that owner:
SessionRecord { session_id, environment_uid = E,
                harness_store = { kind: codex, store_id: X },
                native_session_id = 12345  // once known
}

// Central Session record, if it keys continuation by the public ID:
SessionRecord { session_id, continuation_key = session_id }
```

For a native Session, resolve `S` to the Environment Runtime in environment `E`. The owner’s Session record identifies the harness backend, its actual state store and (once known) native conversation ID. A directory could return an authorized diagnostic view of those facts, but routing to E does not require every client to provide a filesystem path or native ID. For central Sessions, route to the logical sampler scope and use the public ID if that is its durable key.

**Symmetry:** environment UID and native store reference are both placement/continuation facts. Neither is part of the public Session identity. The environment identifies the owner scope to reach; the store reference and native ID tell that owner which harness continuation to resume. Process identity and a connection endpoint are more transient still.

**Change from the earlier decision:** Dispatcher command requests now address a Session ID, and
Dispatcher resolves and fences their placement. Session opening still requires an exact Sandbox,
including for a central Session; the complete directory and environment-independent central Session
model remain unfinished. Existing native Runtime IDs also allow non-UUID strings, so preserving
existing identities needs a deliberate transition or alias policy. See the
[address inventory](#session-address-inventory) and
[`SESSION_DIRECTORY_NATIVE`](task_dag.md#session_directory_native--dispatcher-authority-for-environment-hosted-sessions)
for the implemented and remaining boundaries.

- **One placement authority:** the directory is the source of truth for UUID → assigned Session owner and placement generation. That owner is the authority for harness/store/native mapping. Do not let two independently writable records each claim they select the current owner.
- **Durable reservation:** register the client-chosen UUID and immutable creation request in Dispatcher under a unique constraint. An exact retry resumes/reconciles the same logical reservation; conflicting reuse fails. An owner reservation, if needed, and native creation are distinct from that Dispatcher receipt and may occur later.
- **No invented atomicity:** directory and Session-owner writes may not share a transaction. Persist a recoverable pending reservation, use idempotent owner requests, and resolve uncertain outcomes from durable evidence; never create another native Session merely because a reply was lost.
- **Stale routes fail closed:** carry the exact placement generation and environment UID to the receiver. Cache only with a freshness/fencing protocol; an old owner must reject requests after a completed handoff. A generation checked only against stale local state is not a fence: require a proven lease, authoritative check, or backend-enforced fence, and fail closed when uncertain. On directory unavailability, fail or queue explicitly rather than guessing another owner.
- **Stable identity, explicit moves:** a supported relocation can update placement without changing the UUID, but only after authorization, continuation-store transfer/adoption, old-owner fencing, and an atomic compare-and-swap of placement. This proposal does not assert that existing native harnesses support relocation.
- **Lookup is not permission:** resolving a UUID does not authorize sending input, reading history, waking an environment or executing tools. Avoid leaking placement/store details to unauthorized callers.

The tradeoff is an additional availability and consistency dependency for global resolution. The directory need not be on every hot-path operation if a cached route is demonstrably current and the receiver enforces the placement fence. Session routing and environment-execution routing remain distinct responsibilities.

<a id="attachments"></a>

## Make the difference structural

A native harness's environment and specific harness state store are part of where and how it runs and resumes. A central sampler's environments are optional execution targets. Model those as different variants, rather than one editable list plus scattered backend-specific guards.

```text
SessionSpec {
  session_id: UUID
  harness_placement: oneof {
    environment_hosted {
      environment_uid: E        // required; exactly one
      harness_store: {
        kind: CODEX_APP_SERVER | CLAUDE_CLI
        store_ref: X            // selected durable harness-state store
      }
    }
    central_sampler {
      sampler_scope: C
      attachments: [ ... ]      // 0..N; managed separately
    }
  }
}

// Owner-managed Session record, updated as native creation proceeds:
NativeSession { native_session_id?, creation_state }
```

### Placement, not an attachment

The native harness has exactly one environment and a chosen harness state store. Persist the store selection before starting native creation. No attach, detach, or ordinary edit can silently replace them. A future native multi-environment design is possible but not currently planned.

### Explicit optional attachments

A central Session is independent of any one environment. It may start with none and add or remove named targets; support for more than one is a nearer-term possibility than for native Sessions, not a claim of existing support.

Session and Environment listing and pagination must follow this model; the
[app listing follow-up](../app/TODO.md#session-and-environment-listings) is tracked with
[`SESSION_HISTORY_PRESENTATION`](task_dag.md#session_history_presentation--represent-sessions-without-a-required-sandbox).

A store reference identifies the particular harness state location (for example a Codex or Claude configuration directory) within the exact environment. It may resolve to a path, but should be stable across ordinary process restarts; it is not a PID or a socket. The Session owner uses the harness kind and store reference to locate or start the right harness. Do not assume every store supports concurrent writers or that one app-server per store is already a proven universal rule.

**Two identities, even if their values happen to match:** the public Session UUID is Agentplane’s stable, globally unscoped identity; the harness-native Session ID identifies the conversation within its harness and store. The owner keeps the durable mapping between them. The native ID may be allocated by the harness and may not be known when the public Session is created. The native Session ID is therefore part of the durable Session-to-harness continuation mapping once known. It need not be in the immutable creation request: a harness may choose it during startup. It can be absent before native creation, or unresolved if creation may have happened but the result was lost. If importing an existing native Session, the caller can supply a scoped native reference subject to authorization and conflict checks. A known ID must be durably associated with the public UUID before reporting native creation as resolved; expose uncertain creation when the mapping cannot be proved.

**Current adapters are different:** Claude currently generates a UUID in the adapter and passes it via `--session-id` on first launch; Codex obtains its thread ID from the `thread/start` response; the [upstream app-server request schema](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/v2/ThreadStartParams.json) has no field for choosing that ID. Runtime records the resulting native ID after the handshake. Selecting a stable explicit store reference and handling the unknown-result boundary are proposed contract work, not a claim that the current implementation already guarantees them.

Do not create a second persisted “required attachment” mirroring the environment in native placement. For a common UI or read API, derive a read-only relationship such as `{ environment: E, relationship: native_placement }` alongside central attachment views. It must not make the native environment appear detachable.

**Structure removes invalid representations, not all validation.** The owner validates the placement variant, nonempty exact environment UID and applicable harness-store selection when reserving the Session. A single domain operation enforces immutable native placement across every write path; the receiving Runtime rechecks exact UID and authority. A protobuf `oneof` alone does not enforce required fields, immutability, authorization, or distributed fencing.

| Operation          | Environment-hosted native Session                                                                                                                     | Central sampler Session                                                                       |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Create             | Require one exact environment and select a specific harness state store; durably bind placement. A harness-assigned native ID can be populated later. | Reserve independently of an environment; initial attachments are optional.                    |
| Attach / detach    | Not supported for now. An attempt returns an explicit unsupported or failed-precondition result.                                                      | Explicit, authorized change of a named attachment, subject to implementation capabilities.    |
| Change environment | Not a generic spec edit. A future migration would need explicit semantics for native state, authorization and fencing.                                | Attach/detach changes optional targets; never silently retarget an old UID or in-flight work. |
| Suspend / resume   | The same logical environment can stop and later get a new Runtime incarnation; the required placement stays the same UID.                             | An attachment may become unavailable while the central Session continues.                     |

**For central attachments:** use stable Session-local attachment IDs, target UIDs and revisions; reusing a name after detach creates a new attachment identity. Desired attachment state is distinct from reachability and a live connection. Operations capture attachment ID, revision and target fences, and explicit detachment prevents new calls without claiming an in-flight effect was undone.

**Future note: native harness sandboxing and monitoring.** Today the configured native harnesses are given broad command execution within their environment: Claude uses `bypassPermissions`, while Codex uses `approval_policy: never` and `danger-full-access`. Those settings do not erase the environment's actual OS, container/VM, network, credential or resource boundaries. Harness-specific sandboxing, command filtering, approvals and monitoring may later constrain that access. We are not designing or selecting that policy here; revisit how to represent and enforce it per harness without pretending the Session owner intercepts every native side effect.

Current native settings: [Claude adapter](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/claude/adapter.go) · [Codex adapter](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/codex/adapter.go).

<a id="lifecycle"></a>

## Do not collapse independent state machines

| State                         | Owned by                                   | Meaning                                                                                                                                                                                                                                        |
| ----------------------------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Session existence             | Dispatcher Session record                  | A durable logical reservation, not a running process or successful setup. Central Sessions persist indefinitely for now; app archive does not end one. Whether native Sessions survive deletion of their required environment is under review. |
| Harness observation           | Session owner's harness controller         | Dormant, preparing, ready/running, unavailable or failed, with backend-specific details. A host can serve multiple Sessions.                                                                                                                   |
| Environment desired lifecycle | Environment owner/controller               | Run, suspend/quiesce or delete the exact environment. This is independent of a central Session's desired lifecycle.                                                                                                                            |
| Environment observation       | Session owner, informed by the environment | Availability and current incarnation for the required native placement or a central attachment. It is not a second authority for environment state.                                                                                            |
| Execution operation           | Executor plus Session journal/receipt      | Not dispatched, dispatched/unknown, or proved outcome. Process/output handles belong to an executor lifetime, not to the long-lived binding.                                                                                                   |

### Native harness

Environment Runtime closes Session admission for that incarnation, stops the harness and settles the existing native/Execution fences, then seals and delivers the journal. Reopening a verified successor permits later work under the existing rules. A queued Dispatcher receipt is not Runtime admission.

### Central sampler

The binding becomes unavailable; it does not seal the central conversation or stop provider-only work. A tool call reports target unavailability, subject to any independently authorized wake policy. Do not silently use another environment.

**Deletion:** environment deletion makes its exact UID unavailable permanently. Central attachments become _gone_ until explicitly replaced. A central Session remains addressable; for an environment-hosted harness, continuing requires the actual preserved native store and an explicitly supported recovery/migration path. Existing retained History and its authorization are a separate policy question.

**Central Session lifetime:** no automatic end or cleanup is planned. A central Session persists independently of its attachments and app archive. A future explicit delete operation is a low-priority TODO; its treatment of continuation, work, notices and retained History is not specified now.

<a id="authority"></a>

## Binding is not permission

### Session access grouping and tags

Inbox tags currently live on the Sandbox and authorize sends to its Sessions; separate history tags
and a retained Sandbox record were proposed for history-read grants but are not implemented. Once
Sessions can exist independently of Environments, revisit what the groups label and who owns them,
rather than assuming an attached Sandbox supplies Session access. Review no attachment, shared or
changing attachments, deleted Environments, retained History and inboxes, identity loss, revocation
and migration under [`SESSION_ACCESS_GROUPING_DESIGN`](task_dag.md#session_access_grouping_design--access-groups-across-independent-sessions-and-environments).
Keep send, history read, inbox read/ack and Environment authority distinct. The current Sandbox-based
send rules remain enforced until a reviewed replacement is implemented.

### Authorize each boundary

Check who may create or manage the Session, attach the environment, send commands, wake it, and invoke each environment capability. Recheck live policy and exact target identity before a side effect. A Session address, alias or history reference grants none of these rights.

### Do not upgrade privileges

Record the effective execution principal separately from the requester and broker. An attachment does not copy the environment's credentials into the sampler or silently switch to the host's service account. Delegation must be explicit and bounded by the approved contract.

### Expose honest context

Describe the binding's capabilities, paths, resource constraints and availability to the agent. Notify it when relevant bindings or incarnations change. Distinguish immutable Session instructions from environment-specific context; do not silently rewrite a previously frozen native prompt.

### One environment, shared risk

Multiple Sessions can use the same environment. They may share filesystem, processes, credentials and resource limits. Separate Sessions or different cwd values do not imply isolation from one another.

For a native harness, built-in local tools operate with the environment's actual workload authority. For a central harness, tool access should carry an explicit effective principal through the Gateway to the exact environment. Existing ServiceAccount- and Sandbox-scoped policy remains in force until a reviewed extension replaces it; central Sessions independent of an environment need an explicit authorization scope of their own.

<a id="principals"></a>

## Sessions, ServiceAccounts and environments

**Operator requirement, 2026-11-04:** Sessions using the same Environment must have the same
verified identity. Native placement and central attachment must enforce that invariant against the
actual authenticated workload identity, including concurrent admission and identity changes. A
Session ID, directory, nominal grant or caller-supplied account name is not isolation or proof of
identity. The identity representation and its ownership remain open; see
[`SESSION_IDENTITY_CONTRACT`](task_dag.md#session_identity_contract--session-identity-and-shared-environment-admission).

Candidate: Agentplane owns an agent principal and its permissions. A Session and an environment each have at most one active identity association at a time, and attachment requires an appropriate trust relationship. Kubernetes ServiceAccounts can be the principal’s workload-authentication or external-system accounts, rather than defining the domain identity. Cardinalities, reassignment and the exact implementation remain open.

```text
Session S1 ── principal P ── Environment E1
Session S2 ── principal P ── Environment E1
Session S1 ── principal P ── Environment E2  // central attachment
Session S3 ── principal Q ── Environment E3
// S3 cannot attach E1 while Q != P.
```

### Why the restriction fits today's isolation boundary

Native harnesses can run broad commands in their environment. Co-resident Sessions can communicate through files or processes and can reach ambient egress access and credentials available there. A shared environment is therefore a shared trust domain under the current configuration, even with separate Session IDs, working directories, or action-level policies. Using different ServiceAccounts as labels while routing both agents through the same unrestricted environment would not establish meaningful privilege separation. Explicit agent-to-agent channels are appropriate for trust boundaries; same-environment Sessions can bypass those channels.

Equality is an _admission rule_, not a complete authorization design or an isolation guarantee. Independently authorize who may create a Session as P, operate it, attach E, execute or wake E, and read retained history. Do not permit a caller to select an arbitrary privileged ServiceAccount merely by naming it. If a Session has narrower restrictions than P while it has unrestricted access to E, it may still obtain E’s ambient P authority; enforce meaningful narrower privilege with a separate environment or a real isolation mechanism.

### Why the per-environment ServiceAccount exists

The operator clarified that each Sandbox gets a separate ServiceAccount so PolicyBinding CRs can attach and detach permission sets for that individual agent at the Sandbox level. An account is not just a way to authenticate a Pod: it is currently the subject at which per-agent egress, Actions, inbox and Kubernetes grants are assigned.

**Ownership remains a design question:** should an agent's ServiceAccount always be owned by an
Environment, be managed independently of Environments, or support both modes? A central-sampler
Session may need the same agent identity and permissions while it has no Environment attached and
across attachment changes. Compare a long-lived SA with an Agentplane principal mapped to workload
identities; neither a shared SA nor a new identity service is selected. Define who creates and
deletes the identity, its permission bindings and credentials, and how Session and Environment
attachment lifetimes relate to it.

**Candidate Session spec:** the operator suggests putting the agent ServiceAccount in the Session's
spec. This could make the Session's intended identity explicit at creation and keep it independent
of whether a central Session has an attached Environment. Compare a UID-pinned
`ServiceAccountIdentity` reference in the spec with an Agentplane-principal reference or a separate
Session-owned identity association. Today `SessionSpec` has no identity field: the Dispatcher
opening names a Sandbox, while other records carry an `execution_principal` separately. Decide
whether the field is required, how the caller is authorized to select it, whether it can change,
and how its account lifetime and permissions are managed. A spec reference does not by itself grant
impersonation authority or supply a credential; define authenticated delegation for central work
and validate compatible identity when an Environment is attached. No wire or storage change is
selected by this candidate.

**Initial Sandbox direction:** the operator favors keeping one fixed ServiceAccount for each
Sandbox throughout that Sandbox's lifetime. Bind and fence the exact account identity; if it is
deleted or replaced by a same-name account, do not silently adopt the replacement. This does not
select Sandbox ownership of the account, prohibit sharing an account between Sandboxes, or prevent
normal token rotation and permission changes. Whether a Session has an independent identity, and
whether that Session association can change, remain separate decisions.

**Changing permissions is different from changing identity.** The Sandbox controller already updates its PolicyBinding when `spec.policySets` changes without replacing the running Pod. Policy readers converge independently, so revocation is not globally instantaneous; existing connections and already obtained information have their own behavior. This preserves an individual Sandbox’s permission handle while keeping its current SA and Pod.

**Shared-SA tradeoff:** PolicyBindings name a ServiceAccount subject, and matching grants combine additively. If two environments use the same SA, their bindings’ grants can combine for that subject, so a grant intended for one can apply to the other. A shared SA therefore changes the current per-Sandbox permission granularity unless policy is redesigned with additional trustworthy scope. Sharing may be right for environments intentionally in the same privilege domain; it is not automatically equivalent to today’s per-agent permission assignment.

### Changing the associated ServiceAccount

The operator suggested the association need not be immutable: at most one SA per Sandbox and per Session at any instant does not rule out changing it later. For an ordinary Kubernetes Pod, `spec.serviceAccountName` cannot be changed after creation, so using a different mounted identity means creating a replacement Pod. For VM Sandboxes the launcher Pod obtains its SA through the existing launcher admission and relay; establish the exact VM/VMI replacement procedure separately. A different credential or delegation architecture might support another approach, but the existing Pod-bound egress design does not simply switch SA by editing a policy binding.

Reassignment is not currently supported by the Session contract: the Dispatcher, sampler and retained Runtime record treat their execution principal as immutable, and History pins writer and principal associations. If reassignment becomes a feature, define how the Session and its attached environments change together, handle other Sessions sharing the environment and other environments attached to the Session, fence old work and credentials, and record identity provenance for History. Preserve old authorization/receipt evidence and never replay uncertain effects. Do not present reassignment as a necessary first implementation or a decided product feature.

### Agentplane identity with Kubernetes as a downstream system

The operator proposed that Agentplane’s identity need not be Kubernetes’s ServiceAccount. Treating Kubernetes as a downstream system is plausible: a PolicySet can grant an Agentplane principal Kubernetes access, and a Kubernetes adapter can reconcile an external account and its RoleBindings, just as another adapter might provision a Moodle identity and role. This is a conceptual model, not a requirement to create a separate identity service or make every agent have every external account.

```text
Session ── acts as ── Agentplane principal P ── PolicyBindings ── PolicySets
                              │
             ┌────────────────┼────────────────────┐
             ▼                ▼                    ▼
      Agentplane services   Kubernetes account   Other provider account
      effective principal   + RoleBindings       + provider roles

Environment workload credential ── authenticated association ──► P
```

An Agentplane principal must have a stable, independently managed identity and a verifiable association to the exact environment workload or authenticated delegated caller. A Pod token can remain evidence of a workload’s identity while Agentplane maps the verified workload to P; neither an arbitrary HTTP header nor a Session ID proves P. The shared egress proxy sees the environment workload, not automatically which of several co-resident Sessions issued a request. Under the current broad local execution model, allow only Sessions with the same verified identity in one Environment.

### Existing patterns and possible building blocks

This is commonly called **identity lifecycle management** or **identity governance and administration (IGA)**: define identities and desired access, then provision and reconcile accounts and entitlements across systems. Provisioning, authentication, runtime authorization and credential delivery are distinct functions. An identity provider can authenticate an agent while an independent provisioner reconciles external accounts; neither automatically determines Agentplane's Session/environment authority.

| Building block                       | What it covers                                                                                                                                                                                                         | Boundary                                                                                                                                                                                           |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SCIM 2.0                             | Standard HTTP protocol for managing supported users, groups and related identity attributes in a remote service.                                                                                                       | Requires a compatible provider and target implementation; each system supports different schemas and entitlements. It does not standardize every resource permission or grant a Session authority. |
| OIDC / OAuth 2.0                     | Federated authentication or delegated authorization and token issuance for supported services.                                                                                                                         | Separate from creating and maintaining downstream accounts and resource-specific roles; support and machine-to-machine flows differ by target.                                                     |
| IGA provisioning system              | Identity lifecycle, desired entitlements, reconciliation and a connector catalog. [midPoint](https://docs.evolveum.com/midpoint/) is one open-source example to evaluate.                                              | May be excessive for Agentplane's scale. Verify actual connectors, non-human identity support, operations and ownership before selecting one.                                                      |
| Kubernetes-style provider controller | A controller reconciles desired external resources through a provider adapter; [Crossplane managed resources](https://docs.crossplane.io/v2.4/get-started/get-started-with-managed-resources/) illustrate the pattern. | Generic resource reconciliation is not a complete identity-governance system, and a provider for a specific target and permission shape may not exist.                                             |
| Workload identity                    | Pod-bound tokens or standards such as [SPIFFE](https://spiffe.io/docs/latest/spiffe/concepts/) can authenticate an environment or service.                                                                             | Proving a workload identity alone does not provision accounts, decide Session authority, or make different Sessions in an unrestricted environment isolated.                                       |

**Possible shape:** Agentplane declares the desired principal, relationship to Sessions and environments, and assigned permissions; an adapter reconciles the relevant external account and grants, using SCIM where supported and provider-specific APIs where necessary. The existing Policy Controller already plays a narrower version of this role for Kubernetes RoleBindings. Alternatively an external identity system could be authoritative for principal lifecycle or entitlements, with Agentplane consuming them. Choose one authority for each fact, define ownership of external accounts and grants, and expose reconciliation or failure status; do not silently run competing controllers or assume a vendor owns the whole model.

**Research first:** compare a standard provisioning system or a provider adapter with the existing policy controller by tracing one real provider and Kubernetes end to end. Check non-human accounts, supported grants, explicit trust and delegation, credential delivery without exposing reusable secrets to agents, idempotent reconciliation, revocation and stale/unknown outcomes. External identity products and services named in discussion are examples, not selected dependencies.

Standards and examples: [SCIM protocol](https://datatracker.ietf.org/doc/html/rfc7644) · [one identity provider's outbound SCIM support](https://docs.goauthentik.io/add-secure-apps/providers/scim/) · [midPoint resources and provisioning](https://docs.evolveum.com/midpoint/reference/master/resources/) · [Crossplane managed resources](https://docs.crossplane.io/v2.4/get-started/get-started-with-managed-resources/).

### What the current implementation actually couples

| Boundary                        | Current behavior                                                                                                                                                                                                                                                 | Effect of decoupling                                                                                                                                                                                                                                                                                                                                                     |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Kubernetes API via egress       | The Kubernetes credential uses `projectedWorkloadToken`: the sidecar supplies its own Pod’s token for the API audience, and egress verifies that it is bound to the same Pod as the authenticated hop before substitution. Kubernetes authorizes that actual SA. | The substitution point exists, but it cannot currently substitute an unrelated principal’s token as this source. A broker or new source would need to obtain and protect the chosen Kubernetes account’s credential, authenticate the P-to-account mapping, scope and rotate credentials, and preserve auditing and revocation. Do not expose the token to the workload. |
| Agentplane policy               | `PolicyBinding.spec.subject` is a namespaced SA; egress, Actions and inbox readers match SA subjects. Kubernetes RoleBindings are generated for the SA.                                                                                                          | Bind PolicySets to P or explicitly map P to current SA subjects, and ensure every reader agrees on effective identity and policy. Reconcile Kubernetes-native grants as an adapter; preserve policy freshness, additive rules and same-name UID safeguards.                                                                                                              |
| Agentplane service calls        | TokenReview / workload-auth returns an SA and sometimes Pod UID. Actions, Notification, History, Dispatcher, Environment Service and LLM ingress use these identities in authentication, policy, ownership or attribution. Their contracts differ.               | Translate authenticated workloads or trusted delegated actors into P with method-specific authority. Audit each service’s ownership, idempotency, read scope, Pod proof and provenance; never treat the sampler’s own service account or a caller-provided P as proof.                                                                                                   |
| Lifecycle and persisted records | Sandbox controller owns the per-Sandbox SA and binding; Session and History records pin execution and writer SA identities; the central sampler currently binds an executor and principal.                                                                       | Define P’s independent lifetime and the authenticated P↔workload↔external-account links, preserve historical actor and execution identity, and handle revocation, deletion/recreation, migration, wake and queued work. Keep Session ID, principal, workload credential and external account distinct.                                                                   |

**Scope and sequence:** start by deciding the semantic identity and permission subject; a first implementation might use existing SAs and delegation as adapters without changing all authentication at once. A prototype should follow one concrete agent action end to end, then cover a Kubernetes API request and one Agentplane service with real policy and UID checks. Inventory direct and delegated consumers, credential sources and RBAC reconciliation before estimating the full migration. Support for dynamically selecting an upstream Kubernetes identity is a new credential-brokering capability, not an existing projected-token feature.

**Policy granularity:** a stable principal with individual external or per-environment accounts does not itself decide whether two environments share grants. Specify whether the principal or each environment is the PolicyBinding subject, what an agent is permitted to do through each attachment, and how its authority is propagated. Avoid accidentally widening privileges by unioning per-environment grants or using an unrestricted environment as a privilege boundary.

### The current implementation makes ServiceAccount lifetime the hard part

Today each Sandbox controller creates a ServiceAccount named after that Sandbox and owns it and its PolicyBinding; deletion removes them. Current admission and authorization deliberately check the exact account UID and its Sandbox controller ownerReference. This is closer to one account _per environment_ than an environment selecting a long-lived shared account. A central Session that survives all environments cannot keep operating as an account deleted with one of them, and two environments cannot currently select one existing ServiceAccount just by updating a field.

### Long-lived shared ServiceAccount

Manage principal P independently of environment lifetime, authorize environments to run as P, and allow several environments to reference P. This makes exact ServiceAccount equality straightforward and uses existing policy subjects, but requires changing Sandbox account and PolicyBinding ownership, token projection, UID checks and managed deletion. Its shared subject also combines grants across environments under current policy semantics. Define revocation and who may bind P; do not silently adopt a recreated same-name account.

### Logical principal backed by workload accounts

Give Sessions and environments a durable logical principal while keeping distinct per-environment ServiceAccounts, each explicitly mapped to that principal. Enforce equivalence and allowed delegation at every relevant service; this preserves per-environment token identities but introduces a mapping authority, more policy and a greater drift risk. No additional identity service is assumed.

**Working direction, not settled:** seriously consider an Agentplane-owned principal with independently managed permission bindings and Kubernetes identity as an adapter. Compare that with directly using a long-lived SA, after accounting for desired per-agent PolicyBinding granularity. Keep principal distinct from Session UUID, requester, service broker, workload authentication and journal writer. Establish how all relevant paths enforce the mapping; do not assume immutable association or an online identity-switch feature.

For a central Session with no environment, the principal still exists and the sampler must exercise authorized capabilities under that principal through an appropriate authenticated delegation or broker path. A stored ServiceAccount name is neither a bearer token nor authority to impersonate it; the central sampler’s own ServiceAccount must not silently become the agent’s. Calls made _inside_ an environment use that environment’s actual Pod/relay and egress identity; verify that this agrees with the Session principal and each destination’s authorization contract. Losing an Environment alone does not imply the agent identity disappeared: the human-operator-only fallback applies when the relevant Sandbox-owned ServiceAccount has actually disappeared, not merely when a Session has no attached Environment. The rights of a surviving independent identity still need their reviewed authorization contract.

**Open cases:** which principal owns a Session when the creator is a human or another agent; who grants acting as P; whether, how and when Session or environment SA associations may change; whether absence is a supported state or only a cardinality limit; deletion/recreation and replacement of P; historical access when P is absent; central Session creation without any environment; multiple exact environments for P; and what is allowed when policies are revoked or an environment is replaced. Preserve UID fencing and retained data without making a same-name replacement inherit authority accidentally.

Current source contracts: [Sandbox identities and ownership](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/sandbox_controller/SPEC.md#identity) · [Dispatcher admission and identity](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/dispatcher/SPEC.md) · [egress workload identity](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/egress/README.md#serviceaccount-permissions) · [delegated authorization](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/docs/delegated_authorization.md) · [central sampler identity](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/central_sampler/SPEC.md) · [PolicySets and PolicyBindings](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/docs/adr_policy_sets.md) · [Kubernetes Pod ServiceAccount assignment](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/) · [egress authentication and substitution](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/egress/SPEC.md) · [shared workload authentication](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/workload_auth/README.md) · [inbox identity and scope](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/notification_service/docs/api.md).

<a id="failures"></a>

## Preserve what is known, and what is not

| Event                                                 | Required outcome                                                                                                                                                                 | Do not infer                                                                                   |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Environment unavailable before dispatch               | Refuse with a typed, catchable pre-dispatch result; optionally wake only if separately authorized.                                                                               | That the Session failed or the operation ran.                                                  |
| Connection lost after dispatch                        | Return retained partial output and cursor when available; mark remote outcome unknown; reconcile from evidence.                                                                  | That the process exited, the operation failed, or it is safe to replay.                        |
| Binding detached during a call                        | Prevent new calls on that binding revision. Report the in-flight result if observed; cancellation or cleanup is best effort and has explicit status.                             | That detachment rolls back effects or proves remote work stopped.                              |
| Same environment, new incarnation                     | Adopt only verified exact-UID successor under policy; invalidate old executor sessions/handles and notify the Session before new work.                                           | That old handles identify processes in the successor or unknown prior effects can be replayed. |
| Executor restarts inside same environment incarnation | Detect the separate executor generation/lifetime; abandon or reconcile its handles. An environment UID alone is insufficient.                                                    | That matching environment incarnation proves native process continuity.                        |
| Session owner or JS host fails                        | Resume only from durable continuation and proven effects; expose lost in-memory cells/handles and blocked uncertain work. Other Sessions sharing that host may also be affected. | That a replacement host restores arbitrary memory or authorizes replay.                        |
| Native create outcome is lost                         | Inspect durable/native evidence or proven idempotency; retain an unresolved state if identity cannot be established.                                                             | That no native Session exists and that creating another is harmless.                           |

**Safety boundary:** an environment UID, a successful cancellation request, a disconnected socket, a journal cursor, and a COSI status are each useful facts. None alone proves that an external effect did not happen. Never automatically replay an uncertain mutation.

<a id="ownership"></a>

## Keep authority at the boundary that can enforce it

Session-facing and environment-facing responsibilities may eventually warrant different binary
and deployment boundaries. The current Runner Gateway combines SessionDispatcher and
EnvironmentService; the Sandbox Controller runs separately. Alternatives to review include keeping
the combined process, splitting Session dispatch from Environment management/routing, combining
some Environment responsibilities with Sandbox Controller, and other groupings. This is an open
topology decision, not a selected split or merge; service contracts and ownership do not follow
automatically from which code shares a process. The
[delivery ownership and topology task](task_dag.md#dispatcher_persistence_model_design--choose-delivery-ownership-and-service-topology)
owns the review.

| Owner                           | Owns                                                                                                                                                                                                      | Does not own                                                                                           |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Session owner                   | Harness continuation, native execution and relevant observations; coordinates with Dispatcher Session admission, placement and desired attachments. Placement changes require an explicit fenced handoff. | Another environment's process lifecycle or permission policy. A stored binding is not a durable grant. |
| Session Dispatcher / directory  | Caller-facing admission, live Session records and desired attachments, durable queue/receipts where offered, and authoritative lookup from Session UUID to assigned owner and placement generation.       | Harness state, environment connection ownership, or proof that the model saw an admitted input.        |
| Environment Gateway             | Authorized routing to an exact environment and verified current endpoint/incarnation.                                                                                                                     | Session continuation, changing a binding's desired target, or turning identity into authorization.     |
| Environment Runtime / Execution | Local process and filesystem effects, execution lifetimes, native fences, environment-local harness hosting and quiesce.                                                                                  | A central Session's conversation lifecycle or other environments' state.                               |
| History                         | Retained source evidence and derived conversation views, with actor and environment attribution.                                                                                                          | Harness continuation, scheduling, or a claim that its projection is a replayable harness store.        |

The Dispatcher’s live Session record is the working authority for UUID-to-owner placement, generation and desired attachments. The harness owner manages execution and the continuation reference; specify exactly which principal, reservation, native-ID and idempotency facts belong at each boundary. Define the handoff between these authorities explicitly rather than maintaining competing writable owner mappings. Persist only the observations needed for recovery and audit with their owner. In the environment Runtime, use COSI resources for desired and observed reconciliation state where they fit; keep the Session journal's transaction and cursor with its journal unless a separately justified storage design replaces it. A COSI watch is a wakeup, not a durable effect log or external process fence.

<a id="storage"></a>

## Live control state and retained history

A Session can be usable while its environment or harness is stopped. App-level archive state is separate from whether the backend Session exists; History can outlive both the backend record and its environment PVC.

| Information            | Proposed lifetime and authority                                                                                                                                                                | Examples                                                                                                                                                  |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Live Session control   | One authoritative, durable owner while the Session remains usable, including suspension and temporary unavailability. A lookup resolves its UUID to that owner.                                | Placement and generation, principal, native store and native ID when known, central desired attachments, admission and recovery facts.                    |
| History and provenance | Retained independently of the live object and environment, subject to history retention and read policy. Historical snapshots describe what was true; they do not authorize or route commands. | Conversation and receipts; public ID; harness and Session configuration; time-qualified environment UID, name and namespace; relevant attachment history. |
| Harness continuation   | The harness’s actual store, with a separate lifetime and recovery contract. History alone does not imply resumption is possible.                                                               | Codex or Claude state on an environment PVC; the central sampler’s own durable state.                                                                     |
| Observations           | Derived or ephemeral unless explicitly needed for recovery. Reconcile from durable intent and evidence.                                                                                        | Current process readiness, endpoint and reachability; local COSI status.                                                                                  |

### Where should live control live?

### Dispatcher PostgreSQL control records

Use the Dispatcher’s existing PostgreSQL database for the Session directory and live Session records, including desired attachments. Transactions and constraints can support unique IDs, revisions, admission and queues. The Dispatcher and harness owners must still define who can change each fact and how they reconcile; this does not make the Dispatcher the harness process owner.

### Kubernetes control objects

Represent usable Sessions and perhaps attachments as API objects with controllers and watches. This fits declarative reconciliation and lets native Sessions remain while their environment exists. It adds API and etcd load, RBAC and controller lifecycle concerns; central Sessions need a cluster scope and remain dependent on the API. Garbage-collect live objects when their controlling lifetime ends rather than retaining one per historical Session forever; app archive alone does not establish that lifetime has ended.

### Hybrid

Keep a durable service-owned directory and central attachment state in PostgreSQL, with environment-local desired and observed state in COSI; optionally project a bounded set into Kubernetes when operators or controllers need it. Each fact has one writable authority, with explicit projection and recovery rules. A Kubernetes mirror is not automatically necessary.

### Separate control and history databases

Use Dispatcher PostgreSQL for the live directory and Session control records; use the environment-local COSI store for native reconciliation and the existing History database for retained views. Keeping History's database separate is the current working direction; the later Session-data review below will reconsider that boundary. Avoid a Kubernetes object merely to mirror control state.

**Boundary:** the Dispatcher record is authoritative for live admission, placement and desired attachment state; History retains independently stored evidence and provenance for historical reads. Shared Session UUIDs correlate them without granting either service write access to the other’s database. The detailed ownership of native continuation and attachment reconciliation remains to be specified. Different logical records and services do not require a decision about whether to merge tables, databases, or services later; each would be a separate design choice.

**Consistency:** there is no transaction across the databases. A live Session or command receipt can exist before History has received it. Normal journal export and the existing environment-seal barrier provide the History handoff for managed Sandbox deletion; app archive does not trigger a new cross-database protocol. Decide how History learns enough metadata about reservations with no exported snapshot, and how the UI represents delayed History. Cleanup of a Dispatcher record must not claim that History has recorded facts that were never exported.

### Session-keyed data and storage boundaries

The operator asked to reconsider how Session-keyed state is divided now that Agentplane has a
globally addressed Session and must retain its placement. Today Dispatcher stores openings and
commands, History ingests Session logs and owns retained projections, Notification stores inboxes
and subscriptions, the central sampler retains its conversation and execution state, and the
environment Runtime owns native Session records, journals and harness continuation data. Command
admission and History ingress are distinct ends with different contracts; their data need not share
an owner merely because they name the same Session.

Compare the current modular databases and APIs with moving selected Session state from Environment
Runtime into Dispatcher or another owner, a single database with linked Session-keyed tables, and
other groupings of services and databases. A shared database could offer transactions and foreign
keys where writes actually share a transaction, but neither requires nor proves a shared service or
shared write authority. Conversely, separate stores need explicit handoff and failure recovery.
For each alternative, map canonical records and writers, ingress and command flows, placement and
availability, authorization, scaling and failure domains, native store constraints, migrations and
delete/retention behavior. Preserve journal and native-effect recovery; a retained transcript alone
does not make a native harness resumable. This is a later design question, not a selected move or
database consolidation. The [task DAG](task_dag.md#session_data_ownership_design--review-session-data-services-and-databases)
tracks it independently of the current native-first implementation.

**Database and queue are mandatory:** the combined Gateway already requires the Dispatcher PostgreSQL URL and initializes the database before serving. The repository also contains an unimplemented proposal for a database-free / queue-off mode; this direction supersedes that proposal. Do not build a second stateless command-admission mode. Direct Runtime proxy and Environment Service operations keep their own contracts; this decision does not mean every operation must be queued. Decide separately whether callers need an explicit immediate-or-fail option while the database and queue remain present.

**Existing footprint:** the Gateway’s Dispatcher already requires PostgreSQL for its queue; the central sampler and History each have their own PostgreSQL databases. These are distinct ownership boundaries, not a single shared database or an existing global Session registry. Native runtime COSI state and the harness store remain environment-local.

### App archive and environment deletion are different

### App archive

The app stores each Thread’s name and archive flag in its own database. Its archive operation checks for unfinished work; an idle harness can still be running. Archiving is app-level organization, not a Dispatcher deletion, environment deletion, History flush, or proof that the backend Session can no longer accept commands. Unarchive changes app metadata; it cannot by itself restore a deleted harness store.

### Environment deletion

Today Dispatcher cleanup removes native and central Session opening records associated with the deleted Sandbox UID. Earlier discussion favored removing native live records when their exact hosting environment is gone; the timing and relationship to Notification inbox retirement are now explicitly open for review. Gateway also hosts Environment Service, but Kubernetes deletion and a database transaction are separate; reconcile deletion observed through a watch or relist, including deletion outside the service, and fence races with create and submit. Preserve any evidence or deduplication facts required by their own contracts.

Existing managed Sandbox teardown quiesces the runtime, waits for History ingress to acknowledge the Session seals, and only then releases its finalizer and state PVC. Dispatcher cleanup should use that established lifecycle rather than inventing an app-archive handoff. The Environment Service issues the UID-fenced deletion; the Sandbox controller manages the actual finalizer. Forced deletion or bypassed finalization can lose unexported data and needs an honest outcome, not a claim that History is complete.

Environment loss affects native and central Sessions differently: native Sessions hosted there lose their continuation unless the actual harness store was separately retained and a supported recovery path exists; a central Session survives and the deleted environment’s attachment becomes gone or is removed under a defined policy. Central Sessions do not automatically end; a future explicit delete operation is low priority. Deleting an attached environment cannot delete the central Session. The current Dispatcher cleanup and Notification behavior still tie central records to the original Sandbox; this is a gap against that selected direction.

### Decision: Session and inbox lifetimes after environment deletion

The operator asked to resolve Dispatcher Session-record lifetime and Notification inbox lifetime together. Today the Notification worker [checks the stored Sandbox name and UID and its owner](../notification_service/worker/destination.go), then [retires an inbox and cancels its subscriptions](../notification_service/store/claims.go) after confirming a missing or replaced destination. Dispatcher independently [deletes opening records by Sandbox UID](../runner_gateway/dispatcher/terminal.go). These separate asynchronous decisions can end an inbox because an environment disappeared even when its central Session should survive.

For an environment-hosted Session, compare removing its live Dispatcher record and retiring its inbox when its required environment is deleted with retaining an unavailable Session and its inbox for an explicitly chosen lifetime or until an explicit end. The operator's 2026-11-04 candidate is to give Sessions their own create/delete lifecycle: deleting a required environment leaves the Session, inbox and History present, while the native harness cannot be reached or continued without its actual state and a supported recovery path. An explicit Session delete could instead coordinate cleanup of its associated inbox, History and other data. This is a candidate to review, including which data is retained or deleted, not a selected deletion policy. For a central Session, preserve the selected indefinite Session lifetime: deleting, detaching or replacing an environment alone must not end the Session. Decide whether its inbox should remain active independently as well; the initial recommendation is not to retire it solely because an environment disappeared. Specify whether subscriptions continue collecting while delivery is unavailable, what happens to pending messages and notices, and whether and when any inbox is retired or purged. Suspension, an unavailable Runtime, and a deleted or replaced Sandbox are distinct observations; none proves an in-flight effect failed.

Decide which component owns and observes each transition, how Dispatcher and Notification converge without a cross-database transaction, and how retries, deletion races, exact-UID fencing, authorization and History handoff work. The operator's direction for a retained Session whose Sandbox-owned ServiceAccount has disappeared is human-operator-only read and administration, through an operator-authenticated entry point such as the app using explicitly privileged service credentials, as with other privileged operations. Do not grant access to a same-name replacement account or to other workload callers merely because the original account disappeared. Backend maintenance and reconciliation retain their own narrowly authorized service operations; this direction does not itself select deletion scope or retention. Preserve retained messages and History unless their own reviewed policy permits cleanup. App archive remains separate. Complete forgetting **in Dispatcher when a Session is deleted** and the caller's UUID non-reuse obligation remain the selected identity policy; that does not by itself decide whether other stores retain or delete their data. The separate command-receipt retention question is deferred, with no period selected.

Track the decision and its scoped implementation gate in [the task DAG](task_dag.md#session_and_inbox_lifetime--decide-session-and-inbox-lifetimes).

**What exists now:** History already retains the associated Sandbox namespace, name and (when known) immutable UID, and its projected `Attached` snapshot includes `SessionSpec` (harness, cwd, model, reasoning effort and instructions). The projection can update model and effort, so it is not necessarily an immutable creation-time spec. This is not a complete Sandbox specification or a record of multiple attachment changes; exact provenance, retention and archive semantics still need design. History is asynchronously fed, so a live reservation does not by itself prove an archival snapshot is durable.

Current sources: [History Service](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/history_service/README.md) · [History summary](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/history_service/history_service.proto) · [SessionSpec and Attached](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/proto/session.proto) · [lifecycle projection](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/history_service/projection/lifecycle.go) · [Gateway and Dispatcher](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/README.md) · [central sampler](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/central_sampler/README.md) · [app Thread archive](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/app/README.md) · [Sandbox deletion and sealing](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/sandbox_controller/SPEC.md) · [required Gateway database URL](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/config.go) · [prior optional-queue proposal in the task DAG](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/task_dag.md#dispatcher_persistence_model_design--choose-the-queues-role-always-on-or-optional-in-one-service).

<a id="opening"></a>

## One OpenSession contract

Give callers the same operation for creating a logical Session on either backend. Choose placement explicitly; let the selected owner prepare, reuse or start its harness as needed.

### What opening means today

| Layer               | Current behavior                                                                                                                                                                                                                                   | What success proves                                                                                                                                   |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dispatcher          | Requires a Sandbox name and UID for both harness types, stores an idempotent opening keyed by caller-minted Session ID, and asynchronously delivers it to the selected owner. It reports an opening state.                                         | Dispatcher accepted a durable opening; inspect its state to distinguish queued delivery from owner admission. It does not prove a harness is running. |
| Environment Runtime | Validates the Session, persists its reservation and waits for its local Session owner. Native preparation waits for work; a running harness is not required by this response.                                                                      | The local logical Session was reserved, not that a Claude process, Codex thread or setup completed.                                                   |
| Central sampler     | Currently requires an executor Sandbox and its incarnation, creates durable sampler Session and conversation records, and schedules setup when requested. It may skip executor readiness at Dispatcher delivery but still requires an incarnation. | Sampler state was created or matched; any scheduled setup can still be pending. This is not provider sampling success.                                |

Current implementation: [Dispatcher opening contract](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/dispatcher/README.md) · [Environment Runtime admission](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/session_admission.go) · [central sampler admission](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/central_sampler/service.go) · [central delivery routing](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/dispatcher/central_runner.go).

### What queued and admitted mean

These describe _delivery of the opening request_, not the Session’s active or archived status and not whether its harness is running. Today `QUEUED_IN_DISPATCHER` means the Dispatcher retains the opening awaiting delivery or same-ID reconciliation after an uncertain reply; `DELIVERING_TO_RUNNER` means an owner call is in flight; `RUNNER_ADMITTED` means the selected backend accepted the opening (the name is historical and is also used for the central sampler). A failed or unknown outcome is reported separately. The shared enum also describes command delivery; for an opening, admission means a reservation, not a journaled command or model seeing input.

Auto-wake is one possible way to unblock a queued environment-hosted opening: when the Sandbox is suspended, enabled and authorized wake can request startup; a denied or disabled wake leaves the opening queued. Queueing can also reflect delivery scheduling, an unready or unavailable owner, retries, or an identity check. The actual current blocker and latest wake assessment are separate from delivery state. If the owner is already available, delivery can proceed without wake. The current central path bypasses execution-Sandbox readiness and wake, but still requires a Sandbox and incarnation at opening; the proposed central placement removes that prerequisite.

The chosen direction is that the Dispatcher Session record establishes the logical reservation. The
owner may materialize it eagerly or when work needs it; opening does not wait for materialization.
Keep delivery and error evidence without multiplying user-facing states or requiring an owner call solely to make a Session exist.

### Proposed common contract

```text
OpenSession {
  session_id: caller_chosen_uuid
  spec: { harness, model, instructions, ... }
  placement: oneof {
    environment_hosted { environment_uid, harness_store }
    central_sampler    { sampler_scope, initial_attachments? }
  }
  // Optional setup must identify an appropriate environment when it needs one.
}
→ { session_id, opening_state }  // durable Dispatcher reservation; owner may be unavailable
```

1. **Reserve and route.** The Dispatcher validates identity, authorization, immutable creation request and placement and commits the logical Session. Delivery or materialization at the appropriate owner may be eager or deferred. Exact retries with the same public ID and creation contract return the same opening; conflicting reuse fails. Caller-created UUID is not the harness-native ID.
1. **Materialize at the owner.** The environment Runtime or central sampler accepts the logical Session through its own implementation. The Dispatcher row establishes logical existence; no eager owner reservation is required merely to create it. Current delivery is asynchronous and can be queued while an owner is unavailable; never present Dispatcher acceptance as proof the owner has accepted.
1. **Prepare when needed.** Reuse or start a shared Codex app-server, launch a per-Session Claude process, or create central sampler state as appropriate. The caller need not manage those processes. Report relevant progress and failure separately; opening does not promise native identity, setup, a ready process or provider output. The owner may perform safe work eagerly, but must preserve the same externally visible acknowledgement and idempotency semantics.
1. **Send a command.** Submitting input is a separate durable operation. Reconcile its admission, execution and any uncertain effects using their own evidence. Do not automatically replay a native create or external action after an unknown result.

### Agreed behavior when the owner is unavailable

Here the owner is the environment Runtime for an environment-hosted Session or the central sampler for a central Session; it is not the individual harness process. After request shape, placement and the applicable authorization have been validated, the Dispatcher database commit establishes logical existence. A successful opening does not wait for the owner to be reachable, an environment to wake, History to receive its projection, or a harness-native Session to be created. The Dispatcher must let callers find the accepted Session and observe its progress independently of History.

Unavailable owners and suspended environments are wait and retry conditions. Materialize the Session
at the correct owner before its work requires it, or earlier if useful; wake an environment only
under an authorized policy, which must support eligible unattended inbox notices.

**Materialization outcomes for the first implementation:**

- **Temporary failure:** retain the logical Session and queued work, and retry when conditions allow, subject to the applicable wake policy.
- **Definitive rejection:** retain the addressable Session and expose the materialization error. Do not automatically retry an unchanged request until relevant conditions change. For a rejected configuration, correct the request by opening a new Session with a new public UUID. The original creation request stays immutable; a future edit-and-retry operation is a separate design.
- **Unknown outcome:** an owner might have accepted even though its reply was lost. Reconcile durable owner evidence and, where admission is idempotent, retry admission with the same public UUID and creation request. Do not infer that an uncertain native creation or external effect is safe to repeat.

This contract leaves the classification of errors, retry triggers, queued-work outcomes on definitive rejection, and how clients observe those facts to implementation design. Retaining rejected Sessions may leave unused records, indefinitely for central Sessions under the current lifetime direction. Runtime-configurable fields and their existing update operations are distinct from rewriting the original creation request.

If the exact required environment UID is deleted, fence pending creation and delivery against that deletion and preserve History under its existing retention. When to end a native Session and remove its live Dispatcher record, how its inbox follows, outcome visibility, cleanup and the cross-database handoff remain open.

**Setup and environment wake are explicit policy questions:** opening a central Session without an environment cannot run an environment setup script. If a script is supplied, require an explicit target and define when it runs, its readiness and failure status, and retry behavior. Opening must not require wake for acknowledgement; whether an optional eager attempt may request wake and which component owns wake remain open. Do not assume central and native setup happen at the same time today.

Where callers need progress, expose it through the existing get/watch mechanisms or an equivalent, with clear Dispatcher-accepted versus owner-accepted meaning; do not require a separate visible phase merely because an implementation has one. Show harness/setup readiness separately only where callers need it; avoid inventing a mandatory separate “start harness” API. A future explicit prepare/warm operation can be added if there is a real use case. Before removing the central Sandbox requirement, choose its independent execution principal, prompt context and optional attachment/setup semantics.

### Forgetting a deleted Session

The operator selected complete forgetting in Dispatcher. Callers mint globally unique Session UUIDs
and must not deliberately reuse them after deletion. This is a caller contract, not a server-enforced
lifetime uniqueness guarantee. History remains a separate retained store; forgetting live state does
not delete historical evidence.

**Current behavior, checked at `256a2a7d60`:** [cleanup](../runner_gateway/dispatcher/terminal.go)
deletes openings and dispatches and retains an environment-UID admission fence. It retains no
Session-UUID marker. [Opening retries](../runner_gateway/dispatcher/api.go) recover a retained
receipt without live inventory, but after cleanup an absent UUID takes fresh admission. A request
for the deleted environment UID is fenced; the same Session UUID with a different live environment
UID can be admitted. [History](../history_service/storage/store.go) retains its own Session row and
rejects conflicting principal, writer or environment attribution; accepting a reused UUID in
Dispatcher therefore does not make it a fresh historical identity.

An absent record takes the ordinary fresh-admission path: Dispatcher cannot identify an old retry
after deletion. A request for the deleted environment UID is still fenced. A reused UUID targeting a
different environment can be admitted by Dispatcher but conflict with retained History. Neither an
absent History row nor a History lookup can prove lifetime uniqueness across the two databases.
Do not infer that a missing Dispatcher record means historical evidence was deleted.

<a id="wake"></a>

## Should sending wake an environment?

Unattended inbox notices must be capable of waking an environment. Where that automation belongs, and whether ordinary human sends also wake it, remain open; explicit resume remains independently available and authorized.

### How it works today

The Dispatcher durably queues native openings and commands. Queued work can trigger an automatic wake when `wakeOnSend` and the effective caller’s policy or automatic-wake broker grant permit it. The lifecycle manager checks the exact environment and caller identities, distinguishes operator-requested stops, fences wake generations, and shares a wake budget; an accepted wake request does not mean readiness. Disabled or denied wake leaves work queued. Central sampler openings and inputs bypass execution-environment readiness and automatic wake, though their current opening still requires an executor reference.

Consumers depend on this behavior: the app allows sending to eligible dormant Sessions, Actions document the wake-on-send behavior, and Notification Service accepts inbox messages while the recipient is suspended; the notice normally waits unless authorized wake-on-send causes delivery. The retained inbox entry and the Session notice are distinct.

### Candidate for ordinary human sends: explicit resume and clear rejection

1. **Logical opening.** If the Dispatcher record is sufficient, allow an authorized logical Session reservation for an existing suspended environment without starting it. Environment-dependent validation or setup remains deferred and must be reported honestly.
1. **Work requiring that environment.** For a known suspended native environment, return a typed result such as `ENVIRONMENT_SUSPENDED` with its exact UID and sufficient authorized context to resume. A rejected request must be proven not accepted or dispatched; do not leave it queued for surprise execution after a later resume.
1. **Explicit resume.** A caller with resume authority requests `ResumeEnvironment`, observes readiness, and submits the command. An app or orchestration layer may offer a one-click resume-and-send flow, but the authorization and UI should make that lifecycle action clear. A resume response acknowledges desired state, not readiness.
1. **Central Session.** Provider-only work can continue without an environment. A tool call targeting a suspended attachment gets a typed target-unavailable result; it must not implicitly wake or retarget a different environment. A caller with the right authority may resume that exact attachment’s environment.

**Races and uncertain outcomes:** readiness can change between checking, durable admission and delivery. Specify the admission boundary and fencing so a definite pre-admission rejection proves nothing ran. If an accepted command later encounters suspension, define whether it remains queued or terminates only when non-admission is proven; surface an unknown outcome when delivery may already have happened. Retry the same command ID only under the existing idempotency and no-replay contract. Do not promise that a Kubernetes read and a PostgreSQL insert are atomic.

### Required: unattended inbox notice can wake

Notification Service already persists a notice’s stable command ID, submits it to Dispatcher, and lets Dispatcher own the delivery wait. When the target’s current wake-on-send policy and authorization allow it, queued notice delivery can trigger a wake. Preserve the capability for an inbox notice to wake its recipient without a human or the app; do not require every inbox message to wake unconditionally. The existing inbox entry, notice, Dispatcher receipt, wake request and agent acknowledgement are separate facts.

### A · Keep Dispatcher wake

Keep the existing path: Notification submits a durable notice; Dispatcher queues it and requests authorized wake. This reuses the existing retry, policy and handoff machinery. Decide independently whether ordinary human sends use the same behavior or receive an explicit-resume result.

### B · Notification orchestrates resume

Notification retains the notice, explicitly requests an authorized wake and submits when appropriate. This keeps the policy at the event source, but moves responsibility for wake retries, readiness and the Dispatcher handoff into Notification. It needs an appropriate service authorization and must preserve existing delivery guarantees.

**Revised assessment:** the wanted unattended workflow is a concrete reason for automatic wake. Retaining the existing Dispatcher path may be simpler than moving orchestration and its failure handling into another service; compare both against the actual desired policy before removing anything. Do not assume the existing explicit-resume API grants Notification authority or bypass its distinct broker policy.

In either design, preserve durable notice identity, exact target UID, environment owner’s wake policy, caller and service attribution, requested-stop behavior, wake fencing and bounded retries. A committed inbox message must survive a denied, unavailable or ambiguous wake; reconcile uncertain wake and delivery from evidence without replaying uncertain effects. Coalesce repeated notices where appropriate. Define how a notice for a central Session relates to its optional environment attachments rather than waking one by assumption.

### Human-facing resume and send

The integration app can offer the existing convenience by orchestrating explicit Environment Service and Dispatcher operations. If a click promises that work will survive browser or app restarts, the app can persist its own pending send in its existing database and resume it with a worker; a browser-only flow is simpler if that durability is not promised. This is a proposed app feature, not current behavior.

1. The user sends input to the app. If the app promises durable acceptance, persist the text, exact Session and environment UID, caller/authorization context, and stable command ID before acknowledging that promise. Protect message contents as user data.
1. Using explicitly authorized credentials, the app requests resume for that exact environment and observes readiness. Reconcile an uncertain resume response against current state rather than blindly repeating a possibly applied mutation; a resume acknowledgement is not readiness.
1. Submit the same command ID and payload to Dispatcher, preserving per-Session order. If the response is lost, query Dispatcher’s receipt and follow its documented idempotency and unknown-outcome rules. Persist the result so a restart or replica handoff continues from evidence, not an assumed failure.
1. Show the difference between app-queued, resuming, Dispatcher-accepted and eventual outcome. Once Dispatcher admission is established, Dispatcher remains authoritative for delivery; the app’s queue is an orchestration record, not a second writer to Dispatcher tables.

There is no transaction across app and Dispatcher databases. A durable app queue requires explicit handoff, multi-replica ownership, cancellation, authorization rechecks, retention and ambiguous-result recovery. This is real additional state, so choose it only if the product needs sends to survive disconnection; do not equate an app receipt with Dispatcher acceptance. The app’s operator resume privilege must not silently become a general right for arbitrary Session senders to resume environments. An app-only flow also does not automatically cover Actions or unattended inbox notices.

### Tradeoffs and alternatives

### Explicit human resume

The app or caller requests resume and handles readiness before sending. This can make a human lifecycle choice visible, but does not remove the need for independently authorized unattended notice wakes.

### Keep Dispatcher wake

Sending to an idle-suspended Session and delivering an unattended inbox notice can each be a durable operation. This retains wake grants, requested-stop handling, identity rechecks, budgets, retry and ambiguous wake outcomes; the desired notice behavior gives that machinery a concrete purpose.

Durable queueing and automatic wake are independent choices. Even without automatic wake, the Dispatcher may need durable receipts, idempotency, ordering and retries for commands accepted while a running environment briefly becomes unavailable. Whether to reject only _known suspended_ environments or also queue while they are starting or transiently disconnected is a separate contract decision. A durable inbox can accept a message while wake is denied or unavailable; when configured and authorized, unattended notice delivery must be capable of waking the recipient. Decide which component schedules it.

**Migration if selected:** inventory callers and UI controls, update Actions and inbox/notice contracts, decide how existing queued work and wake-on-send settings are handled, and remove obsolete wake-only grants, fields and status only after their consumers and rolling versions have moved. Keep explicit-resume authorization, UID fencing, race handling and any independently needed rate limits. Audit defaults and policies rather than silently granting resume to senders.

Current behavior: [Dispatcher queue and wake](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/dispatcher/README.md) · [environment lifecycle manager](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/environments/lifecycle/README.md) · [inbox and notice behavior](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/notification_service/docs/api.md) · [app send and explicit resume](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/app/README.md).

<a id="api"></a>

## What a caller would do

Illustrative domain API, not chosen protobuf names or a promise of current support. All mutation calls carry caller-generated request IDs; retries bind the same ID to the same request.

```text
// Central: the Session is useful without an environment.
S = createSession(placement = centralScope, sessionId = uuid, harness = sampler)
submitInput(S, commandId, "Reason about this plan")

attachEnvironment(S, name = "dev", target = envUID1,
                  expectedSessionRevision = r, requestId = attachId)
attachEnvironment(S, name = "test", target = envUID2, ...)
exec(S, binding = "dev", bindingRevision = r1,
     expectedEnvironmentIncarnation = i1, operationId = op1, command = "...")

// Native: the harness placement requires the selected environment.
N = createSession(placement = nativeEnvironment(envUID1), sessionId = uuid,
                  harness = claude, cwd = "/workspace/project")
submitInput(N, commandId, "Inspect the project")
// N requires envUID1 in its native placement; attach/detach is unsupported.
```

| Operation      | What success acknowledges                                                                                     | What it does not promise                                                                                                            |
| -------------- | ------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Create/reserve | The Dispatcher durably accepted the logical Session identity, placement and immutable creation contract.      | Backend materialization, harness process, setup, executor readiness or model work. Report backend progress separately where needed. |
| Attach/detach  | The Dispatcher committed the desired binding revision or removal and its authorization decision at that time. | Target is live, future access remains authorized, or in-flight work was undone.                                                     |
| Submit input   | Exactly which boundary durably admitted the command, keyed by command ID.                                     | The harness consumed it or the model saw it; report those only with their own evidence.                                             |
| Execute        | Result or the precise admission/unknown state for the bound target and incarnation.                           | Automatic replay on disconnect or successful cancellation of unobserved effects.                                                    |

<a id="migration"></a>

## Get there without a flag day

This is an illustrative progression, not a blanket dependency chain. The authoritative
[task DAG](task_dag.md#13-delivery-boundaries-apis-and-harness-identity) records independent
decisions, implementation outcomes and conditional dependencies. In particular, broader identity
provisioning and service/database regrouping do not gate the initial zero-Environment Session;
attachment History delivery does not gate the initial attachment UI.

**1. Decide and document the object contract.**

Review global UUIDs, a common OpenSession reservation contract, authoritative Session-to-owner resolution, required versus optional bindings, central authorization scope, and deletion semantics. Map existing native and central opens to the same concepts without claiming they already implement them.

**2. Resolve Session UUIDs to an authoritative placement.**

Update the task DAG and prior proposal to reflect the mandatory Dispatcher database and retire the queue-off mode; review the separate database-independent integration-tier idea on its own merits. Add the Session directory to Dispatcher PostgreSQL and define its reservation/unknown-outcome contract; add a placement generation and typed native or central destination. Route native Sessions to their Runtime in the exact environment UID and central Sessions to a stable sampler scope. Preserve native UID checks, same-ID idempotency, caller authority, queued-versus-owner admission, and existing Sessions. Do not guess an owner from a mutable name.

**3. Remove the central Session's fixed environment dependency.**

Let central reservation and command delivery work when there is no environment. Separate optional setup/working-directory behavior from Session creation and define how an initial executor is chosen when setup requires one. Preserve current prompt freezing and explicit policy checks.

**4. Add explicit optional bindings.**

Begin with one named binding, then support multiple when the underlying tool surface can target each correctly. Persist desired changes in the Dispatcher Session record and reconcile them with the Session owner; expose observed status; require explicit target and exact fences. Add detach and per-binding failures without silently changing targets.

**5. Propagate through tools and presentation.**

Teach agents, CLI and UI to inspect/select bindings and observe changes. Keep native-local behavior truthful, define environment instructions and credentials, and avoid presenting unsupported remote capabilities.

**6. Revisit remaining placement and cleanup.**

Only after the contracts work, assess whether to regroup services, expose a separate binding resource, migrate native stores, or retire redundant names/fields. Those are consequences to evaluate, not prerequisites.

**Data and rollout.** Preserve existing Session IDs, native stores, history, effective principals and idempotency across any selected transition. The user has said existing Sandboxes can be deleted for earlier Runtime cutovers; do not assume that automatically authorizes deleting retained Session or history data for a future design. A fresh-state cutover is possible only after its scope is established.

**Acceptance examples:** two Sessions in the same environment can reference the same Codex store with distinct native IDs; an unknown native-create result is not replayed; native Session survives ordinary Runtime restart; a shared Codex host can stop without losing Session identities; a central Session opens and accepts provider-only work with zero environments; adding two exact bindings routes each call correctly; deleting one environment does not delete the central Session; an in-flight unknown effect is not replayed; same-name replacement is not adopted; unauthorized attach/wake/execute is refused; native placement cannot be detached or changed through attachment or generic edit APIs.

<a id="decisions"></a>

## Decisions this proposal asks for

**Highest-value decisions before implementation:** choose the identity and authority relationship between Sessions, environments and ServiceAccounts, including ServiceAccount lifetime. Also specify the Dispatcher/backend handoff and unattended wake policy. The operator has selected Dispatcher-owned logical reservation, ongoing central Session lifetime, and eventual History recording of attachment transitions; implementation details remain below.

**Priority A · Who can own and use a central Session with no environment?** Today the Dispatcher derives the Session execution principal from a Sandbox-owned ServiceAccount and History scopes normal reads to the retained execution principal. A central Session with zero environments needs a durable principal or ownership scope independent of any attached environment, plus explicit create, read, send and attachment authorization. Recommend retaining the real initiating principal separately from the sampler’s service identity and checking environment-specific permissions at each attachment and operation; choose the precise owner identity and lifetime before claiming zero-environment support.

**Agreed · What Dispatcher reserves.** The Dispatcher’s committed Session row establishes logical existence, including when the environment Runtime or central sampler is unavailable. Its placement and request are authoritative; the owner creates runtime/continuation state on demand and retains the native ID where appropriate. Report a later materialization rejection separately; correct a rejected creation request by opening a new Session for the first implementation. Decide how a lost native ID is recovered without replay. No cross-database atomicity or mandatory wake is implied.

**Agreed · Attachment transitions in History.** Recording every attachment transition is a correctness requirement, but not high priority. Today History retains one Sandbox association, insufficient for a central Session with zero or many environments. Design durable, time-qualified provenance and historical read authority. If the History view is deferred, identify the temporary coverage gap and avoid claiming complete provenance; consider capturing transition evidence at mutation time because missed changes may not be reconstructible later.

**Agreed · Central Sessions persist.** There is no automatic Session end or garbage collection. App archive is presentation; deleting an attached environment does not delete the central Session. **Low-priority TODO:** consider a future explicit delete operation and its treatment of in-flight work, notices, continuation, History and identity reuse.

**Priority E · Which events can wake?** Unattended inbox notices must be capable of authorized wake; the existing Notification → Dispatcher path already supports it. Recommend keeping that path initially unless a concrete ownership or simplification case supports moving it. Decide whether human sends and central attachment tool calls may also cause wake, and how requested stops, policy and no-replay behavior apply.

**1 · Dispatcher Session database and History boundary** Working direction: persist globally addressed live Session records and desired attachments in Dispatcher PostgreSQL; keep History in its separate database. Decide the precise ownership split with harness owners, the cross-database publication and managed-deletion cleanup, availability, idempotency retention and read behavior during propagation. The combined Gateway requires the Dispatcher database and keeps queue-backed admission; do not add the previously proposed database-free / queue-off mode. Future table, database or service consolidation is a separate decision.

**Wake policy · Where should automatic wake live?** Required capability: unattended inbox notices can wake an eligible environment. Compare keeping the existing Dispatcher wake path with Notification-owned explicit wake orchestration; also decide ordinary human-send behavior, accepted-work races and queueing for starting/disconnected targets. Preserve policy and authority in either design.

**OpenSession · What does opening promise?** Agreed: the Dispatcher row creates the logical Session for both placements without waiting for owner availability; harness startup and native creation may happen later with the owner. Decide setup timing, optional wake, failure classification and any readiness/warmup API without requiring an executor for a central Session that does not use one.

**2 · Are central attachments durable by default?** I recommend durable desired attachments when explicitly added; reachability and active streams stay ephemeral. Ad hoc one-call environment access could remain a separate explicit capability rather than silently creating a lasting attachment. Native placement is a separate required relationship.

**3 · What is the default target?** I recommend explicit attachment selection for central environment tools, with an optional explicit per-Session default. Native-local tools naturally use the required placement environment. Never auto-select an available replacement.

**4 · What survives native environment deletion?** I recommend that historical evidence can remain if its retention and read policy permit it, but native continuation is unavailable unless its actual store and a supported recovery path are retained. Decide whether and how native continuation stores can be archived or transferred; don't infer it from a transcript.

**5 · How much attachment authority is delegated?** Decide the central Session's security scope, who may attach, which capabilities/principal can be selected, and whether revoked policy stops or only prevents new operations. Keep caller identity, broker identity and effective principal distinct.

**6 · What does an agent learn as environments change?** Decide how environment descriptions and skills reach the model, how changes are announced, and which instructions are frozen for native harnesses. A new binding cannot retroactively alter an immutable prompt.

<a id="session-address-inventory"></a>

## Session address inventory and follow-up work

The inventory records the implemented opening lookup and Session inbox diagnostics. Subscription
creation now uses a Session-only request, as merged in [#14103](https://github.com/agentydragon/ducktape/pull/14103).
A Session ID is the public address; an exact environment UID remains meaningful
for native placement, queued-work and execution fencing, authorization, and historical provenance.
An environment-scoped operation or list can still name an environment. Selecting an environment
when creating a native Session is placement, not a compound Session address. No change below
removes History or actual harness state.

| Surface and current behavior                                                                                                                                                                                                                                                                                                                                                                                                                                | Follow-up and owning task                                                                                                                                                                                                                                                                                                                                 |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Dispatcher opening and command API](../runner_gateway/dispatcher/session_dispatcher.proto) requires an exact Sandbox on open; submit, model, effort and interrupt now address a Session ID and can carry an exact UID authorization precondition. [Opening storage](../runner_gateway/dispatcher/store.go) couples the Session to a required Sandbox; [deletion](../runner_gateway/dispatcher/terminal.go) removes openings by Sandbox UID.                | Finish [native Session authority](task_dag.md#session_directory_native--dispatcher-authority-for-environment-hosted-sessions) and remaining [command authority](task_dag.md#delivery_destination_contract--send-commands-to-a-session-id), preserving resolved UID fences.                                                                                |
| The [browser client](../app/frontend/sandboxes/client.ts) reads openings by global Session ID through the app's operator-authenticated Dispatcher facade. The wire endpoint is [`GET /v1/sessions/{session_id}/opening`](../runner_gateway/dispatcher/session_dispatcher.proto); test callers use [`SessionDispatcherClient.get_session_opening`](../runner_gateway/dispatcher/testing_client.py). App creation still composes presets and Thread metadata. | **Implemented:** the Sandbox-scoped AppService lookup is removed in favor of Dispatcher by Session ID. Review creation separately: a Sandbox can remain a selected native placement without defining Session identity.                                                                                                                                    |
| [App command delivery](../app/frontend/threads/command_delivery.ts) submits new commands by Session ID; the [Session CLI](../cli/sessions.go) likewise uses Session ID for send and interrupt. Previously retained browser commands keep their original Sandbox target.                                                                                                                                                                                     | Preserve the original fence of saved or accepted work and no-replay recovery; remaining broker and authority work is in [Session-ID command addressing](task_dag.md#delivery_destination_contract--send-commands-to-a-session-id).                                                                                                                        |
| [Session Actions](../action_service/session_delegation/contract/inputs.go), their [executor](../action_service/session_delegation/executor.go), and [orchestration](../action_service/orchestration_v1/sessions.go) carry a target environment for opening and Session commands.                                                                                                                                                                            | Review command schema and grant scope in [Session-ID command addressing](task_dag.md#delivery_destination_contract--send-commands-to-a-session-id); retain creation placement and delegated actor/effective-caller checks.                                                                                                                                |
| [Session inbox diagnostics](../notification_service/docs/api.md#session-inbox-diagnostics) use Session ID for `GetInbox` and inbox, entry-summary and subscription watches. Storage enforces one inbox per Session, retains destination keys and cursor/notice state, and refuses conflicting existing inboxes without discarding them.                                                                                                                     | Implemented. Keep current workload ownership and exact Sandbox UID checks; diagnostics do not establish command send or wake authority.                                                                                                                                                                                                                   |
| [Subscription creation](../notification_service/notifications.proto) accepts only `session_id` ([#14103](https://github.com/agentydragon/ducktape/pull/14103)); the [handler](../notification_service/api/subscribe.go) resolves the Dispatcher opening and retains exact Sandbox UID, caller ownership, Runtime membership and source checks.                                                                                                              | Implemented. Sessions without a Dispatcher opening cannot create or resubscribe.                                                                                                                                                                                                                                                                          |
| Notification message sends address a Session ID and resolve its Dispatcher opening before checking a scoped send grant against the exact Sandbox. The [delivery worker](../notification_service/worker/deliver.go) submits by Session ID with an exact-UID authorization fence. Inbox payload reads, acknowledgement and retirement address the Session ID.                                                                                                 | Implemented for current Sandbox-bound Sessions. Preserve exact UID fences, unread messages, idempotency and authorized unattended wake; [central Sessions without Environments](task_dag.md#central_session_notifications--inboxes-for-sessions-without-environments) need separate identity and lifetime work.                                           |
| [Session instructions](../session_instructions/instructions.go) and `AGENTPLANE_SESSION_ID` carry only the public Session ID into native and central harness tools. Central conversation instructions are frozen at admission and reused on resume.                                                                                                                                                                                                         | Implemented: the CLI reads the raw ID and has no reader for the old Sandbox-shaped `AGENTPLANE_DESTINATION` value. Existing central Sessions retain their frozen prompt, which may still show the old Sandbox reference and variable name; use `--session ID` for subscription commands or open a new Session. No prompt or History rewrite is performed. |
| [Central sampler opening](../central_sampler/sampler.proto) embeds a required executor destination and incarnation; central Session storage is separate for conversation/continuation.                                                                                                                                                                                                                                                                      | Session ID remains the Session address; move executor choice to optional attachment/setup under [Session/environment design](task_dag.md#session_runtime_model_design--sessions-harness-placement-and-execution-attachments). Identity and 0..N attachment behavior remain open, not a mechanical rename.                                                 |
| [History reads](../history_service/history_service.proto) already address a Session by ID; [ingress](../history_service/ingress/ingress.go) binds writer, principal and Sandbox provenance, and [FindSessions](../history_service/reads/summaries.go) filters by Sandbox. [Thread views](../app/frontend/threads/client.ts) currently require a Sandbox, while [groups](../app/frontend/threads/thread_groups.ts) organize by it.                           | Keep ID-keyed reads and useful environment filters. Adapt writer authority, optional/multiple provenance and UI in [History and presentation](task_dag.md#session_history_presentation--represent-sessions-without-a-required-sandbox), including eventual attachment transition history.                                                                 |
| [Gateway environment routing](../runner_gateway/gateway/sandboxes.go), [Runtime control](../env/runtime/control/environment_runtime.proto), execution, managed deletion and quiesce are environment-scoped; [system-test Agent](../system_tests/agent.py) and fixtures also carry Session destinations.                                                                                                                                                     | Keep exact environment/workload fences for calls to a particular Runtime and environment operations. Update test helpers and assertions with their corresponding address changes; do not replace a safety fence with a UUID-only lookup.                                                                                                                  |

The [subscription CLI](../cli/notifications.go) accepts `--session ID` or reads the raw ID from
`AGENTPLANE_SESSION_ID`. It does not read the former `AGENTPLANE_DESTINATION` envelope. Existing
central Sessions retain their frozen Sandbox-shaped prompt; that prompt may refer to the old variable,
so use `--session ID` for subscription commands or open a new Session to receive current guidance.
Neither stored prompts nor History are rewritten. Existing subscriptions keep their stored destination
fences, cursors and notices; reads, renewals and delivery continue
([rollout contract](../notification_service/README.md)). This does not grant message-send or wake authority.

The older [delivery design](../docs/runner_delivery_and_harness_design.md#destination-checks-and-authority)
contains useful authority analysis, but its caller-supplied `(Sandbox UID, Session ID)` destination
has been superseded. Session ownership, History and Notification changes must preserve the required
records and service boundaries.

<a id="discussion"></a>

## Discussion log

A concise record of how this proposal is evolving. Entries distinguish the operator's direction, working proposals and unresolved questions; inclusion here does not by itself approve an implementation.

**2026-11-02 · One model for two relationships**

The operator asked for a model covering central Sessions with zero, one or potentially many attached environments, and native harness Sessions inherently bound to the environment in which the harness runs. The proposal separates the Session's home and continuation from its environment bindings; a native home has a required local relationship. It must remain honest about which harnesses can actually support remote execution or relocation.

**2026-11-02 · Where does native continuation live?**

The operator asked whether addressing should include the harness state location and native Session ID, such as an environment, Codex store and native thread ID. For a central sampler, they suggested the sampler might simply use the public Session ID as its own key. The assistant initially recommended a home-scoped public address with a private mapping to store and native identity. The location of the actual continuation state remains a necessary durable fact even if it is not part of the public identifier.

**2026-11-02 · Global Session identity and placement lookup**

The operator challenged why the environment UID was in the public home address while the store was not, and proposed globally unique, unscoped Session UUIDs resolved to the responsible environment or sampler. The assistant agreed that both environment and store are placement/continuation facts, and revised the working proposal: resolve a UUID through an authoritative Session-to-home directory; the selected home resolves its harness store and native ID. The operator's suggestion is the current direction for discussion, not a finalized directory implementation or migration approval.

**2026-11-02 · “Home” meant too many things**

The operator asked whether “Session home” meant an environment or central harness, or a particular harness in an environment. The assistant acknowledged that the draft conflated responsibility, harness placement, a running process and continuation storage. The updated working terminology distinguishes a Session owner (the responsible component scope), a harness backend and placement, its durable continuation reference, and any replaceable running harness process. This is a clarification for discussion, not a decision to add a separate owner service.

**2026-11-02 · Native placement versus central attachments**

The operator noted that native harnesses currently have broad command execution within their own environment, and is not currently planning more than one environment for those Sessions. Multiple central-sampler attachments are somewhat higher priority. The assistant recommends a placement variant with exactly one required, fixed native environment and a separate central variant with zero or more optional attachments. The operator's last example said “central sampler” where the surrounding description implies the native harness; this interpretation is recorded as an assumption. Future harness sandboxing, command filtering, approvals and monitoring remain a note, not a design selected here.

**2026-11-02 · Which specific native harness state?**

The operator asked whether environment-hosted placement must also name the specific Codex or Claude state store and its native Session ID. The assistant agrees that environment plus harness kind is insufficient: the placement identifies the harness kind and particular durable store, while the Session owner retains the native conversation ID once known. The process is replaceable. Codex supplies its thread ID during `thread/start`; the current Claude adapter generates an ID and passes it to the CLI. A public Session can be reserved before a native ID exists, and a lost creation response must remain unresolved until evidence proves the mapping. The exact store identity, concurrency rules and native-ID exposure remain design questions.

**2026-11-02 · Can the client choose a Codex ID?**

The operator asked whether Codex supports client-chosen IDs. For the app-server interface Agentplane uses, the upstream [`thread/start` schema](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/v2/ThreadStartParams.json) has no caller-selected thread ID; the response supplies it. A client supplies a known thread ID to resume a thread; the JSON-RPC request ID is a separate correlation identifier. Thus a public Session UUID needs a durable mapping to the Codex thread ID, and a lost creation result must be reconciled without blindly starting another thread. This finding is scoped to the app-server API, not every possible Codex interface.

**2026-11-02 · Our ID and the harness’s ID**

The operator agreed that Agentplane must retain a distinction between its own public Session ID and the harness’s native ID, and noted that this separation is healthy in any case. The proposal treats them as identities with different ownership and scope, even when a particular harness permits equal values. The Session owner records their mapping together with the harness store; a native ID may be unknown until creation completes.

**2026-11-02 · Where does the information live, and when is a Session historical?**

The operator raised PostgreSQL-backed Gateway state versus Kubernetes objects for Session attachments, and asked that Kubernetes not accumulate objects for Sessions users have finished with. Keeping usable native Session objects while an environment and its PVC exist may be reasonable. They described the frontend as a History viewer plus a command sender for live Sessions, and suggested retaining a useful environment/Session-spec description alongside historical records after Sandbox deletion. The assistant added storage options, initially proposed an archive handoff, and documented what History already retains. The later correction below separates app archive from backend cleanup.

**2026-11-02 · Dispatcher database, separate from History**

The operator favors a Session database connected to the Dispatcher. They noted the attraction of eventually merging the Dispatcher and History Session tables, but prefer the conservative boundary of keeping their databases separate for now. The proposal now puts live Session directory and desired attachment records in Dispatcher PostgreSQL, keeps History authoritative for retained records in its own database, and initially proposed an asynchronous archive handoff. The subsequent discussion corrects that handoff: app archive is separate from backend Session cleanup. This does not foreclose future consolidation.

**2026-11-02 · App archive is not Session cleanup**

The operator clarified that archive is currently an operation in the app database, and suggested that Dispatcher mostly delete native Session records when their hosting environment disappears; the Gateway also serves Environment Service. The assistant checked the app and Sandbox controller contracts and withdrew its proposed backend archiving state and app-archive handoff. The plan now separates app archive, Dispatcher cleanup, and the existing managed-environment seal and History acknowledgement. Cleanup on direct Kubernetes deletion, create races, retained receipts, and central Sessions need explicit treatment.

**2026-11-02 · Converging OpenSession**

The operator asked whether opening Sessions should have a common shape across central and environment-hosted harnesses, noting that opening can involve bringing up a harness and creating a Session on it. The assistant recommends one logical Dispatcher reservation API with typed placements and explicit admission stages; process startup, shared-host reuse and native creation remain backend responsibilities. The current native Runtime already reserves lazily, while the central sampler currently requires a Sandbox and incarnation, persists its own conversation and can schedule setup. Setup timing, wake and central Sessions without executors remain design decisions.

**2026-11-02 · What do pending and admitted mean?**

The operator asked whether these opening states are about auto-wake. The assistant clarified that they track delivery of a stored opening to its backend owner; waking a suspended environment is only one possible blocker or action. The existing Dispatcher reports queue, delivery and admitted states separately from blocker and wake assessment. The proposal now also asks whether a Dispatcher-owned Session record can be sufficient until work arrives, rather than assuming owner-side opening is always required.

**2026-11-02 · No databaseless Dispatcher mode**

The operator concluded that, with a mandatory Dispatcher Session database, there is no reason to support a databaseless / queueless mode. The assistant verified that the current combined Gateway already requires PostgreSQL at configuration and startup; the optional queue was an unimplemented task-DAG proposal, not an existing supported mode to remove. The plan now retires that proposal, keeps the existing queue-backed contract, and leaves an immediate-or-fail option and any narrower database-independent integration tests as separate questions.

**2026-11-02 · Automatic wake versus explicit resume**

The operator questioned whether Environment Service / Dispatcher should automatically wake an environment for Session work, or instead reject work targeting a suspended Sandbox and ask the caller to resume it first. The assistant favors explicit, independently authorized resume and a typed pre-admission rejection for known suspension, while treating the details of accepted-work races, transient disconnects, inbox notices and higher-level resume-and-send as open. Existing wake-on-send and its consumers remain current behavior until a reviewed change.

**2026-11-02 · App-owned resume and send**

The operator proposed retaining human-facing auto-resume-and-send by implementing the queue and orchestration in the integration app. The assistant agreed: an app-owned durable queue can persist a user request, request authorized resume, wait for readiness and submit a stable command ID to Dispatcher. The two databases require a recoverable handoff; app acceptance and Dispatcher acceptance remain distinct. A browser-only flow is an option if survival across disconnects is not required, and non-app callers need their own policy.

**2026-11-02 · Unattended inbox wake is wanted**

The operator clarified that unattended inbox notices should be capable of triggering wake. The assistant revised its earlier explicit-resume recommendation: retaining Dispatcher wake is a credible, potentially simpler option because Notification already persists the notice identity and submits it to the Dispatcher, which can queue and wake under current policy. Notification-owned wake orchestration is an alternative. The app-only solution does not cover this requirement; wake policy and human-send behavior remain open.

**2026-11-02 · Remaining decisions before implementation**

The operator asked what else needs clarification. The assistant prioritized a central Session principal independent of an environment, the Dispatcher/backend reservation boundary, History provenance and authorization for zero or multiple environments, central Session end and cleanup, and the policy and ownership of unattended wake. The assistant recommends keeping the first implementation narrow and treating migration, identity collision, lost responses and no-replay as concrete contract cases rather than inventing additional services or Session state machines.

**2026-11-02 · Reservation, History and central lifetime**

The operator agreed that the Dispatcher Session row establishes logical existence, wants all attachment transitions recorded in History eventually as a correctness improvement but not high priority, and wants central Sessions to persist indefinitely for now. A possible explicit delete operation is a low-priority future TODO, not an implementation prerequisite.

**2026-11-02 · Session, environment and ServiceAccount identity**

The operator proposed associating each Session with one ServiceAccount and permitting attachment only to environments belonging to that account. They noted the coupling to Kubernetes identity but also that two agents with different privilege levels should arguably not share an unrestricted environment: they could communicate through it and use its credentials outside explicit Agentplane channels. The assistant agrees this is a sound trust-domain starting point, while leaving the identity model undecided. Current Sandbox-owned ServiceAccounts have environment-scoped lifetimes and cannot directly provide a long-lived shared principal without changing ownership and provisioning. The plan compares direct shared ServiceAccounts with an explicitly mapped logical principal and records separate authorization and central-delegation questions.

**2026-11-02 · Why each Sandbox gets its own ServiceAccount**

The operator clarified that the per-Sandbox SA provides an individual agent-level subject for attaching and detaching permission-set CRs. They floated allowing the Sandbox or Session SA association to change over time while keeping at most one at a time, and noted that changing a Pod’s mounted identity may be inconvenient. The assistant verified that PolicyBinding changes already work without Pod replacement, while Kubernetes does not allow changing an existing Pod’s ServiceAccount. It also identified that shared SAs combine current additive grants, potentially losing the per-Sandbox permission handle. SA reassignment and the identity model remain open.

**2026-11-02 · Treat Kubernetes as an external identity provider**

The operator suggested that Agentplane could own its identity model independently of Kubernetes: environment access to the Kubernetes API goes through egress substitution, and a PolicySet adapter could provision a Kubernetes SA and RoleBindings much like an external Moodle account and role. The assistant agreed the model is plausible and recorded the present coupling: the API token is currently the same Pod’s projected SA token, PolicyBinding subjects are SAs, and multiple Agentplane services authenticate and authorize by SA. A principal-to-workload mapping and a new external credential source or broker need explicit verification; decoupling can be staged and does not automatically require a new identity service.

**2026-11-02 · Standard identity provisioning patterns**

The operator asked whether reconciling desired agent identities, accounts and roles across systems has an established name or implementation, noting examples of identity providers and downstream services. They clarified that the named products were incidental, not selected targets. The assistant identified identity lifecycle / governance and provisioning, SCIM for supported identity resources, OIDC/OAuth for authentication and authorization, and connector or Kubernetes-controller patterns as separate building blocks. The plan keeps product choice and the authority for identity and entitlement facts open.

**2026-11-02 · Opening while the Session owner is unavailable**

The operator asked what “backend” meant in the proposed opening contract. The assistant clarified that it means the responsible environment Runtime or central sampler, not necessarily a running harness process, and proposed acknowledging logical Session creation once the Dispatcher commits it even if that owner is unavailable. The operator agreed. Owner materialization and its errors remain observable separately, and work can wait for an authorized wake, including wake for unattended inbox notices. The discussion leaves error correction, wake ownership, uncertain-effect recovery, exact deletion cleanup and central Session authority open; it does not claim the proposed directory is implemented.

**2026-11-02 · Rejected Session materialization**

The assistant proposed retaining a logically accepted Session when its owner later rejects materialization, showing the error, and retrying temporary failures or reconciling unknown admission outcomes without replaying uncertain native effects. For the first implementation, the creation request stays immutable; a corrected configuration uses a new Session UUID. The operator agreed, including the tradeoff that rejected records can remain visible, potentially indefinitely for central Sessions. Error classification, queued-work outcomes and native-ID recovery still need a concrete contract.

**2026-11-03 · Session-address audit and plan burn-down**

The operator confirmed the chosen public address is the Session UUID and the live Session database
belongs with Dispatcher, then requested an inventory of Sandbox-plus-Session consumers with linked
follow-up tasks. The inventory distinguishes public Session addressing from native placement,
execution fences, authorization and provenance. The plan removes the older compound-address
recommendation and records completed prerequisites; the detailed central principal and attachment
contracts remain open.

**2026-11-03 · Deleting Dispatcher state means forgetting**

The operator clarified that deleting a Session's Dispatcher record was conceived as forgetting that
it existed there, and asked to discuss the options without selecting a retention or UUID-reuse
policy. The proposal now compares complete forgetting, a minimal UUID marker and consulting
History. It distinguishes today's environment-UID fence from a Session-UUID marker, and records
the effects on retained History, exact retries, globally unique UUIDs, concurrent admission and
availability. No marker, retention lifetime or UUID-reuse policy is agreed by this clarification.

**2026-11-03 · Commands require a Dispatcher Session record**

The operator said that with a required Dispatcher database, commands should always have a
Dispatcher record. In the context of the command-authority discussion, this selects an existing
Dispatcher Session record as a prerequisite for Session-targeted commands, including privileged
callers. It does not select Session deletion or UUID retention semantics, send/wake grants, or a
requirement to queue every operation. Dispatcher and the Gateway now enforce the Session-record and
proxy boundaries; deletion fencing and Session-ID-only command addressing remain open.

**2026-11-03 · Deletion races and initial send authority**

The operator agreed to fail commands known not to have left Dispatcher when the environment is
deleted, report an in-flight outcome as unknown when Runtime admission or effects cannot be
established, and never redirect accepted work to a replacement. They also agreed that the first
environment-hosted cutover should preserve current direct and delegated caller checks, scoped
App/Notification/orchestration trust, and separate wake checks including unattended notices.
The concrete deletion fence, where outcomes remain observable, queued-grant revocation and central
Session identity remain open.

**2026-11-03 · Native Session authority and forgetting**

The operator agreed that a Dispatcher commit establishes logical Session existence and selects its
placement. For native Sessions the existing opening record is the authority; materialization at its
owner can occur eagerly or on demand. The central placement remains a separate future implementation.
Dispatcher forgets a Session after deletion; callers must not deliberately reuse its globally unique
UUID. Dispatcher cannot recognize an old retry after its record is removed, and retained History may
still reject a conflicting writer. No permanent UUID marker or cross-database uniqueness check is added.
The operator also wants central agents eventually to learn about zero or more environments and may
later let them request authorized attach/detach operations. Their tool and authority contracts remain open.

**2026-11-03 · Session and inbox lifetimes after environment deletion**

The operator observed that Notification retiring an inbox when its Sandbox disappears is analogous
to the question of how long Dispatcher keeps Session records after environment deletion, and asked
to record the relationship as an unresolved decision. The existing implementation removes the
Dispatcher opening and independently retires a stale inbox after confirmation. Review the lifetime
and coordination of both records together, distinguishing native placement from central attachment.
The selected indefinite lifetime of central Sessions and complete forgetting on Session deletion
remain unchanged. Command-receipt retention is a separate deferred topic; no retention period was chosen.

**2026-11-04 · Session data ownership and independent lifetimes**

The operator suggested revisiting the boundaries among Session-keyed data and services, including
Dispatcher commands/openings, History ingress, Notification inboxes and Runtime-local state. A
single database with linked tables and moving some Runtime data into Dispatcher are possibilities,
not decisions. They also proposed for consideration that Sessions have their own create/delete
lifecycle: removing a native Session's environment could leave the Session and associated inbox and
History present but unable to run, while an explicit Session delete could coordinate cleanup. The
retention policy, deletion scope, authorization and data/service placement remain to be reviewed.

**2026-11-04 · Access after the Sandbox-owned ServiceAccount disappears**

The operator's direction is that retained Sessions and inboxes become accessible for human
inspection and administration through privileged service credentials, as in other operator paths.
Disappearance of the original ServiceAccount must not transfer its authority to a replacement or
other workload caller. This access direction does not decide Session deletion or retention.

**2026-11-04 · Agent identity ownership across Environment attachments**

The operator asked whether agent ServiceAccounts should always be Sandbox-owned or could have an
independent lifetime. A central-sampler agent may have no attached Environment yet still need to
keep its identity and permissions. Compare independently managed SAs, Sandbox-owned SAs and a
separate principal with mapped workload accounts; ownership, cardinality, authority and credential
delivery are not selected. The privileged human fallback above applies when the relevant SA is
actually gone, not to every Session without an attached Environment.

**2026-11-04 · Identity in the Session spec**

The operator suggested making the ServiceAccount part of a Session's spec. This is a candidate for
an explicit, stable Session identity, including for a central Session with no Environment. Account
ownership, exact identity form, selection and change authority, and credential delivery remain to be
designed; a Session spec does not itself authenticate its caller or create an account.

**2026-11-04 · Shared-Environment identity boundary**

The operator requires Sessions using the same Environment to have the same verified identity:
co-resident agents have shared ambient access, including to the egress proxy and placeholder tokens.
Enforce this for native placement and central attachment, including concurrent admission, rather than
treating Session IDs or directories as isolation. The account representation and lifecycle remain
open; placing an account reference in the Session spec is a candidate, not authorization by itself.

**2026-11-04 · Fixed Sandbox ServiceAccount**

The operator favors the simpler initial model: a Sandbox keeps one ServiceAccount throughout its
lifetime. Preserve the account's exact identity rather than adopting a same-name replacement. This
narrows the earlier discussion of changing a Sandbox's SA; it does not settle independent Session
identity or account ownership, nor does it freeze permissions or credentials.

**2026-11-04 · Session-facing APIs and service topology**

The operator reiterated that callers interacting with a Session should not be bound to an
environment. Placement and environment attachment remain separate concerns. They also asked to
reconsider process and deployment grouping as those responsibilities diverge: separate Session and
Environment binaries and deployments, merging some Environment responsibilities with Sandbox
Controller, and other arrangements are possibilities. No topology was selected.

**Still open**

Specify Dispatcher directory availability and stale-route fencing, the ownership split with harness owners, managed-deletion cleanup and any missing History provenance, how reservations spanning directory and owner are recovered, and how existing non-UUID native IDs transition. Decide whether and when a Session may move, whether store/native references are exposed to callers, and central Session authorization without an attached environment. Existing native destination and authority contracts remain in force until a reviewed change replaces them.

**Maintenance rule:** append or amend this log whenever the discussion changes the model. Record the gist, any reversals, and remaining decisions; update the main proposal too. Keep proposed ideas separate from operator-approved decisions and implemented behavior.

<a id="sources"></a>

## Sources and boundaries

This is a proposal grounded in repository `devel` at `d1aeff8ca2`. It describes target behavior and explicitly marks current limitations; it does not claim an implementation or rollout.

- [Existing Session, harness placement and execution attachment design](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/session_runtime_model.md) — scenarios, capabilities, instruction delivery, failure questions.
- [Session/runtime design task](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/task_dag.md#session_runtime_model_design--sessions-harness-placement-and-execution-attachments) and [native identity decision](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/task_dag.md#harness_identity_design--decide-private-session-to-store-and-native-mapping) — current scope and remaining native mapping decisions.
- [Central sampler specification](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/central_sampler/SPEC.md) — current fixed target, lazy executor acquisition, incarnation and recovery limits.
- [Native Session protocol](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/docs/session_protocol.md) · [Environment Runtime](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/SPEC.md) · [Execution specification](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/execution/SPEC.md).
- [Dispatcher contract](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/dispatcher/README.md) · [multiagent authority and Session access direction](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/multiagent_communication.md).
