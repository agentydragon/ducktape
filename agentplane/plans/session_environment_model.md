# Sessions & environments

> Working design and discussion record, with current behavior reconciled against `devel` at
> `12530ca33e` on 2026-11-04. This document distinguishes implemented behavior from proposals.
> The address inventory's prior baseline was `devel` at `9b44c17e60`; the subscription
> entry was reconciled against current `devel` at `a7b398dfc9` after [#14103](https://github.com/agentydragon/ducktape/pull/14103) merged.
> The original investigation used the source revisions linked at the end. Discussion entries
> are historical: later decisions and corrections supersede earlier suggestions. The shorter
> [Session/runtime model brief](session_runtime_model.md) remains useful context; this document
> records the more recent decisions and unresolved details.

## Decision status and implementation readiness

The Session-resource and native-first authority slices are implemented. The broader model still
has unresolved identity, attachment, recovery, notification and retention contracts.

**Selected model direction:** public, unscoped Session UUIDs distinct from harness-native IDs; one
required environment for current native Sessions; central Sessions eventually able to have zero or
more environment attachments; Dispatcher PostgreSQL establishes logical Session existence without
waiting for its owner; History remains separately stored; app archive is not Session deletion; and
authorized unattended inbox notices can wake an environment. Record attachment transitions in
History eventually. The implemented baseline below does not yet provide environment-free central
Sessions, attachments, or the full identity and notification model.

**Completed prerequisites:** mandatory Dispatcher PostgreSQL and queue-backed admission; the
[opening/recovery inventory](session_opening_recovery.md) maps existing ownership;
[#14026](https://github.com/agentydragon/ducktape/pull/14026) fences uncertain Runtime reservation
writes; [#14030](https://github.com/agentydragon/ducktape/pull/14030) requires canonical UUIDs for
new Dispatcher Sessions; [#14031](https://github.com/agentydragon/ducktape/pull/14031) checks the
exact Sandbox UID for new message inboxes; [#14248](https://github.com/agentydragon/ducktape/pull/14248)
lands typed placement and the `CreateSession`/`GetSession`/`ListSessions` resource cutover; and
[#14314](https://github.com/agentydragon/ducktape/pull/14314) retains Dispatcher Session records
after deletion of their required Environment.

**Implemented Session resource:** [#14248](https://github.com/agentydragon/ducktape/pull/14248)
replaces the opening-specific resource and API with typed `SessionPlacement` and
`CreateSession`/`GetSession`/`ListSessions`. The read projection omits the private `SessionSpec` and
instructions; the [address inventory](#session-address-inventory) preserves the earlier wire shape
as a pinned snapshot. The [Dispatcher outcome contract](../runner_gateway/dispatcher/SPEC.md#native-opening-outcomes-and-dependent-commands)
defines owner materialization and dependent-command fencing. [#14314](https://github.com/agentydragon/ducktape/pull/14314)
now retains the Session and exact placement when its required Environment is deleted; exact retries
and authorized reads report that deletion. This does not restore the execution target or establish a
permanent Session-ID marker after a future explicit Session deletion.
All Session-targeted commands now require a Dispatcher Session record, including privileged callers and
interrupts. Gateway permits direct Runtime Session creation and mutation only to the combined
Gateway/Dispatcher workload; its read-only diagnostics and separate Executor service have their own grants.
The separate deletion-outcome contract for command receipts remains incomplete. Public commands address
the Session by ID; Dispatcher resolves and fences its placement.

**Implemented placement and Session-resource cutover (#14248):** backend selection and placement live
in the shared `SessionPlacement` message at
`SessionSpec.placement`. Its oneof selects `environment_hosted`, with the exact environment name and
UID plus native harness selection, or `central_sampler_v1`, with one exact, fixed required
`executor_environment`. Dispatcher `Session.placement` projects the stored value without exposing the
spec or instructions. `Session` also contains `session_id`, timestamps and `SessionStatus`; its
materialization is `WAITING`, `IN_PROGRESS`, `ACCEPTED`, `FAILED` or `UNKNOWN`, with reason, delivery
blocker and wake assessment where available. After required-Environment deletion, the independent
`required_environment=DELETED` status does not rewrite owner materialization. Status reports progress at the placement owner, not
harness readiness. `ACCEPTED` means durable owner admission, not a running harness or completed
History projection.

Logical creation is named `CreateSession` in Dispatcher, App, Environment Runtime control and
central Sampler. Dispatcher reads use `GetSession` and `ListSessions`. The CLI uses `sessions create`,
`sessions get` and `sessions list`; old `open` and `get-opening` command aliases and the former
`OpenSession`, `GetSessionOpening`, `ListSessionOpenings` and `SessionOpening` contracts have no
compatibility path. The distinct streaming `Executor.OpenSession` remains an execution-connection
handshake. Central sampler V1 still requires one fixed executor environment for placement and
admission, and its current notification and wake paths remain Sandbox-backed. #14314 retains the
Dispatcher Session row after that Environment is deleted; it does not add an alternate executor,
attachments or environment-free central Sessions. Native harness store selection and harness-native
ID mapping remain future work, and the central principal remains unresolved.

The hard cutover coordinated Dispatcher, sampler, app/CLI, Notification, History and matching
environment/runtime callers; old Sandbox callers and binaries do not overlap the new contract.
Dispatcher migration 11 converts retained placement and migration 12 renames `session_opening` to
`session`; central sampler migration 10 converts its retained requests. Dispatcher migration 13
adds the required-Environment deletion marker without dropping Session rows. History migration 8 keeps
legacy harness facts as `historical_harness` when exact placement cannot be reconstructed, and
migration 9 renames `opened_position` to `recorded_position` while preserving values, ordering,
sequence ownership and indexes. The History watch cursor is `recorded_after` and its frame is
`SessionRecorded/session_recorded`; old wire names have no compatibility path. These migrations
preserve Session, central continuation, command, notification and History data, identity, cursors and
effect evidence; they do not requeue work or replay effects. This does not implement the future
environment-independent model or resolve identity/retention questions.

**Implemented authority:** Dispatcher PostgreSQL is the live Session authority for current native
and central V1 placements. Do not add a second competing writable directory. The current contract
does not provide environment-free central Sessions, optional attachments, a central principal, or
native harness-store and native-ID mapping.

**Still require design:** central Sessions with no required environment, optional attachment
semantics and authorization, the central principal and its authority, notification/wake behavior,
lifetime and retention after environment loss, and cross-store deletion and History semantics. The
current Dispatcher table is the directory for current native and central V1 placements, not a
general attachment or environment-independent Session directory. Central sampler V1 still requires
one fixed executor environment. The explicit [central Session environment-independence task](task_dag.md#central-session-environment-independence)
tracks these prerequisites and the later data-preserving migration.

Decisions still needed include the Session/environment principal and privilege relationship;
attachment authorization, revision and History publication; inbox and History behavior after
Environment deletion; explicit Session-deletion and command-receipt retention; and which component
wakes a suspended environment under which authorization. Since 2026-11-05,
Agentplane-owned identities with SPIFFE workload authentication are the selected
direction ([log](#discussion)). Independent identities may share one Kubernetes
account, and no account is auto-provisioned per identity. The discussion proposed a
shared low-privilege default account plus separate or temporary accounts on request;
exact account and binding/grant shapes, provisioning and identity-provider choices
remain proposed or open. The existing ServiceAccount-first Session direction stands
until cutover. The related
[Agentplane principal decision](task_dag.md#agent_principal_model_design--agentplane-principals-and-external-identities)
covers identity provisioning and trust boundaries.

<a id="central-session-environment-independence"></a>

## Central Session without a required executor environment

**Later work; not part of Central sampler V1.** V1 stores the sampler conversation centrally but
requires one exact executor environment in `SessionSpec.placement.central_sampler_v1`. That
environment remains required for V1 placement and execution admission. Its Sandbox identity is
still used by current notification and wake paths. On Environment deletion, Dispatcher now retains
the Session row with `required_environment=DELETED`; this does not add an alternate executor, allow
environment-free admission or make the central Session independent of that required Environment.
The executor is not an optional attachment. The later task is linked from the
[task DAG](task_dag.md#central-session-environment-independence).

Before removing this requirement, settle the central Session's principal and create/read/send
authority independently of an attached environment; define Dispatcher and sampler admission when
there is no executor target; and decide whether the selected 0..N attachment direction will ship now
or in a separate step. Attachment names, per-operation authorization, exact UID fences and
attach/detach history must be explicit. Define what inbox subscriptions, pending messages and
unattended notifications do with no attachment and how an authorized wake targets a later exact
environment. Resolve Session and inbox lifetime/retention after an environment is removed. History
reads must remain available without a live environment or directory lookup.

This is a coordinated wire and storage cutover across Dispatcher, sampler, app/CLI, Notification,
History and environment callers. Old `SessionSpec` encodings and old Sandbox/runtime binaries are
not a supported compatibility tier. Migrations must preserve central Session IDs, provider
conversation, commands, cursors, effect evidence, inbox data and retained History; preserve legacy
harness provenance when exact placement is absent instead of fabricating an environment. Do not
requeue accepted work, replay an uncertain provider/executor effect, or turn missing History into a
success claim. Keep placement and data migration separate from any later authorization change.

**Exit:** a reviewed contract and migration plan for opening and addressing a central Session with
zero environments, optional attachment semantics and authorization, inbox/wake behavior, lifetime
and retention, and History access. Automated acceptance proves migrated central and History records
remain readable, an environment-free Session admits and continues only allowed work, exact attached
environment fences reject replacements, and uncertain effects are never replayed. No implementation
starts before the principal/auth and lifetime decisions are resolved.

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
unknown, and never redirect work to a replacement environment. #14314 retains the Session row and
fences the deleted environment UID; command-receipt outcome reads after cleanup remain unresolved.

Dispatcher reads now target the Session ID through
[`GetSession`, `GET /v1/sessions/{session_id}`](../runner_gateway/dispatcher/session_dispatcher.proto)
and return the minimal `Session` projection.
The [browser adapter](../app/frontend/sandboxes/client.ts) calls that endpoint through the
operator-authenticated app facade; test callers use
[`SessionDispatcherClient.get_session`](../runner_gateway/dispatcher/testing_client.py).
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
Dispatcher resolves and fences their placement. The coordinated `CreateSession` contract still
requires an exact environment, including for a central Session; the typed resource does not provide
the proposed environment-independent central model. Existing native Runtime IDs also allow
non-UUID strings, so preserving existing identities needs a deliberate transition or alias policy.
See the [Dispatcher Session contract](../runner_gateway/dispatcher/SPEC.md#sessionspec-placement-cutover)
and [address inventory](#session-address-inventory) for implemented and remaining boundaries.

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

| State                         | Owned by                                   | Meaning                                                                                                                                                                                                                    |
| ----------------------------- | ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Session existence             | Dispatcher Session record                  | A durable logical reservation, not a running process or successful setup. Environment deletion retains the Session row and exact placement; app archive does not end one. Explicit Session deletion remains unimplemented. |
| Harness observation           | Session owner's harness controller         | Dormant, preparing, ready/running, unavailable or failed, with backend-specific details. A host can serve multiple Sessions.                                                                                               |
| Environment desired lifecycle | Environment owner/controller               | Run, suspend/quiesce or delete the exact environment. This is independent of a central Session's desired lifecycle.                                                                                                        |
| Environment observation       | Session owner, informed by the environment | Availability and current incarnation for native placement or the required executor of central sampler V1. Future central attachments are not part of V1. It is not a second authority for environment state.               |
| Execution operation           | Executor plus Session journal/receipt      | Not dispatched, dispatched/unknown, or proved outcome. Process/output handles belong to an executor lifetime, not to the long-lived binding.                                                                               |

### Native harness

Environment Runtime closes Session admission for that incarnation, stops the harness and settles the existing native/Execution fences, then seals and delivers the journal. Reopening a verified successor permits later work under the existing rules. A queued Dispatcher receipt is not Runtime admission.

### Central sampler

The current central V1 Session retains its logical Dispatcher record when its required executor
Environment is deleted, but V1 has no alternate or optional target. Report the required Environment
as deleted and fail closed; do not infer that provider-only work remains available or use another
Environment. The proposed environment-independent central model may later permit that capability.

**Deletion:** environment deletion makes its exact UID unavailable permanently. #14314 retains the
Dispatcher Session and immutable placement for both native and current central V1 Sessions, records
`required_environment=DELETED`, and prevents a same-name replacement from inheriting work. This
retains logical identity; it does not recover native continuation or provide a new central execution
target. Continuing a native Session requires its actual preserved store and a supported recovery
path. Existing History and its authorization are a separate policy question.

**Central Session lifetime direction:** no automatic end or cleanup is planned; the Session persists
independently of attachments and app archive. Current central sampler V1 still requires one exact
executor Environment, and deletion retains its logical Session record without adding another target.
A future explicit delete operation is a low-priority TODO; treatment of continuation, work, notices
and retained History is not specified now.

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

Session command, interrupt and history-read authorization must also be reconsidered for Sessions
without an Environment. The older Sandbox-only orchestration rule is not a complete contract;
[`SESSION_ACCESS_GROUPING_DESIGN`](task_dag.md#session_access_grouping_design--access-groups-across-independent-sessions-and-environments)
and [`DELIVERY_DESTINATION_AUTHORITY_DESIGN`](task_dag.md#delivery_destination_authority_design--assign-session-send-authority-and-internal-fences)
track the open target and authority choices.

## Sessions, ServiceAccounts and environments

**Operator requirement, 2026-11-04:** Sessions using the same Environment must have the same
verified identity. Native placement and central attachment must enforce that invariant against the
actual authenticated workload identity, including concurrent admission and identity changes. A
Session ID, directory, nominal grant or caller-supplied account name is not isolation or proof of
identity. The initial identity is a Kubernetes ServiceAccount; lifecycle and authorization details
remain open. See
[`SESSION_IDENTITY_CONTRACT`](task_dag.md#session_identity_contract--session-identity-and-shared-environment-admission).

Initial direction: the Session uses a namespaced SA name as its durable principal, consistent with
Kubernetes RBAC and name-based PolicyBindings. A replacement object of the same name is the same
named principal with a new UID. Authenticate and record the current UID for provenance and fence
old Pod credentials, observations and already accepted operations; the name alone is not proof.
A Session initially selects an authorized existing SA without transferring ownership. The operator
is comfortable deferring Session-managed SA creation and deletion; if selected later, Dispatcher is
the proposed owner.
An Agentplane-owned principal mapped to external accounts remains a separate future option.
Cardinalities, reassignment and the exact implementation remain open.

Name-based ownership has an explicit consequence: an authorized same-name SA replacement can
receive the principal's PolicyBindings, Kubernetes RoleBindings and Session access. Restrict who may
create or recreate that name, create workloads using it, request its tokens and grant permissions;
an SA name supplied by a caller is not authentication or authority. A managed-SA reconciler could
recreate an accidentally deleted object while the Session still desires it, but whether to do that
and how to request durable revocation remain open. For an unmanaged SA, disappearance makes the
principal unavailable until an authorized owner restores it. Already admitted work and old tokens
remain fenced to their actual incarnations, not automatically replayed or handed to a replacement.
Current Session and History implementations pin SA UIDs; coordinate the contract and data changes
before allowing name-based ownership, preserving recorded actor and writer UID provenance.

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

**Operator direction, 2026-11-04:** use SAs as agent identities first, while leaving a later
Agentplane-owned principal and Kubernetes account adapter open. A caller can select an authorized
existing SA that the Session does not manage. Automatic creation and deletion of a separate SA
for every Session is desirable later but not required now. A central-sampler Session may need its
identity and permissions without an Environment. If Session-managed SA provisioning is selected
later, Dispatcher is its proposed owner; creation, deletion, bindings, credentials, sharing and
attachment lifetimes still need a contract. See
[`SESSION_MANAGED_IDENTITY`](task_dag.md#session_managed_identity--provision-a-session-managed-sa).

**Candidate Session spec:** the operator suggests putting the agent ServiceAccount in the Session's
spec. This could make the Session's intended identity explicit at creation and keep it independent
of whether a central Session has an attached Environment. Decide whether the named SA reference
belongs in the spec or a separate Session-owned association. Today `SessionSpec` has no identity field: the Dispatcher
opening names a Sandbox, while other records carry an `execution_principal` separately. Decide
where the identity intent and resulting UID belong, how the caller is authorized to select it,
whether it can change, and how its account lifetime and permissions are managed. A spec reference does not by itself grant
impersonation authority or supply a credential; define authenticated delegation for central work
and validate compatible identity when an Environment is attached. No wire or storage change is
selected by this candidate.

**Initial Sandbox direction:** the operator favors keeping one fixed ServiceAccount name for each
Sandbox throughout that Sandbox's lifetime. A same-name replacement has a new object UID but the
same named principal; old workload and operation evidence must not be treated as its evidence. This does not
select Sandbox ownership of the account, prohibit sharing an account between Sandboxes, or prevent
normal token rotation and permission changes. The Session identity is independent of Environment
lifetime; whether its identity association can change remains a separate decision.

**Changing permissions is different from changing identity.** The Sandbox controller already updates its PolicyBinding when `spec.policySets` changes without replacing the running Pod. Policy readers converge independently, so revocation is not globally instantaneous; existing connections and already obtained information have their own behavior. This preserves an individual Sandbox’s permission handle while keeping its current SA and Pod.

**Shared-SA tradeoff:** PolicyBindings name a ServiceAccount subject, and matching grants combine additively. If two environments use the same SA, their bindings’ grants can combine for that subject, so a grant intended for one can apply to the other. A shared SA therefore changes the current per-Sandbox permission granularity unless policy is redesigned with additional trustworthy scope. Sharing may be right for environments intentionally in the same privilege domain; it is not automatically equivalent to today’s per-agent permission assignment.

An agent that needs additional permissions can request an approved PolicyBinding for its existing
SA; this can grant Kubernetes roles, Agentplane Actions or scoped egress credential substitution.
The current [temporary-grant guidance](../action_service/docs/agents/kubernetes.md#request-a-temporary-policybinding)
uses a Kubernetes admin Action, and [`AGENT_PERMISSION_REQUESTS`](task_dag.md#agent_permission_requests--an-action-type-and-approval-view-for-permission-requests)
tracks a dedicated request and approval view. A grant applies to the SA and therefore to every
workload or Session able to use it; it is not per-Session isolation. A separate SA for extra
Kubernetes permissions could be used without changing an Environment's Pod-bound identity. Decided
2026-11-05: the egress proxy impersonates the second account, under an identity's use rule, through
the `kubernetesServiceAccount` credential source
([identity plan](identity_and_kubernetes_accounts.md#kubernetes-execution)).
The GitHub bot case can instead use an existing EgressCredential, a matching host/method/path
egress rule and a PolicyBinding for the effective caller; external GitHub permissions and those of
any shared SA remain shared rather than becoming a unique external account for each Session.

### Session-managed SA and native creation order

Today the form selects PolicySets for a new Sandbox, whose controller creates and owns its SA and
binding; Sessions created in that Sandbox share it. Initially a Session may use an authorized
existing SA without taking ownership or changing its grants. Preserve the web's preset and editable
PolicySets where it creates an Environment or an explicit grant, and show when the chosen SA is
shared. Creating a new Session-managed SA and letting Dispatcher reconcile its identity intent,
binding, observed UID and status remains a later option, not a prerequisite. No new identity
service is selected. Actual delegation for central-sampler calls must prove both actor and effective
caller; an SA reference alone cannot authenticate or authorize a call.

An environment-hosted Session spec currently requires an already existing exact Environment UID;
central sampler V1 also requires one fixed executor Environment. One candidate resolves the ordering:
create an **unbound, non-running Environment resource**, persist the Session with that placement and
managed-SA intent, provision the SA, bind the exact Environment to that SA, then start its workload
and materialize the Session. The Session could instead use an authorized existing SA already bound to
an Environment. Pre-provisioning an SA and other reservation approaches remain alternatives; this
sequence is not yet a chosen API or implemented behavior. Using an already bound Environment and
its existing SA does not require a Session-managed SA or an unbound-Environment creation API.

The review must settle who may claim and bind an Environment, how competing claims are serialized,
who cleans up abandoned unbound Environments or partially provisioned SAs, and how retries and
uncertain cross-system writes recover without adopting foreign objects or reusing an old UID's
evidence. A valid same-name replacement represents the same named principal. An unbound
Environment must not start with a default or unverified SA. Once bound, reject unbinding or changing
the SA name for that Environment's lifetime: its retained workspace or other state may contain
sensitive data. Deleting the SA object does not erase the binding; a same-name replacement is the
same named principal, while old workload evidence stays fenced. A bound Environment cannot receive
extra grants merely because a different Session is created. Decide what the user sees while
provisioning, and how the form offers creation versus selection of an existing identity. Reconcile Session deletion with any other Session or
Environment using its managed SA: blocking deletion or explicitly changing identity ownership are
possible policies, not yet selected. Deleting a Session never deletes an unmanaged SA.

SPIFFE URI mTLS identifies the Environment Runtime and Gateway on the control connection; it does
not supply the agent's outbound authority. Egress currently authenticates a Pod-bound SA token and
uses the Pod's projected credentials. Binding before workload start can preserve control mTLS while
selecting the workload identity; changing the SA of an already-running Pod needs replacement or a
separately designed credential mechanism. See
[`ENVIRONMENT_IDENTITY_BINDING_DESIGN`](task_dag.md#environment_identity_binding_design--environment-sa-binding-and-session-creation-order).

**Later option: materialization cost.** Per-Session SA management is not required initially. A
distinct live SA, PolicyBinding and possibly native RBAC objects
for every retained Session could accumulate in Kubernetes. Compare eager creation with reserving
the Session's SA name and materializing it only when an authorized operation needs it. The operator
expects fewer than 10,000 Sessions in existence at present and considers eager SAs likely acceptable;
this is a working scale assumption, not a measured capacity limit. Eager creation is a reasonable
first candidate, with lazy creation revisited if observed scale warrants it. Measure the
object counts and control-plane costs rather than assuming a bottleneck. A Session that only needs
provider-side work might avoid a Kubernetes object, but the current delegated-authentication path
requires the effective SA to exist and match its UID, and an Environment uses a Pod-projected SA
token. Deferring an SA until explicit Kubernetes API access would therefore not support every
current Agentplane call. Decide whether to materialize at first Agentplane authorization, represent
a reserved-but-unavailable identity until then, or design an independent logical principal; the
latter is more than lazy creation of a Kubernetes object. Keep selected PolicySets and authorization
intent durable without granting access based only on an unverified name. See
[`SESSION_SA_PROVISIONING_DESIGN`](task_dag.md#session_sa_provisioning_design--when-to-materialize-session-sas).

### Changing the associated ServiceAccount

The operator earlier suggested the association need not be immutable, but now requires the initial
Environment binding to reject unbinding or changing its SA name once bound. A Session's own identity
reassignment remains a separate question. For an ordinary Kubernetes Pod, `spec.serviceAccountName`
cannot be changed after creation, so a different mounted identity also needs a replacement Pod. For
VM Sandboxes the launcher Pod obtains its SA through the existing launcher admission and relay;
establish the exact VM/VMI replacement procedure separately. A different credential or delegation
architecture might support another approach, but the existing Pod-bound egress design does not
simply switch SA by editing a policy binding.

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

**Scope and sequence:** start by deciding the semantic identity and permission subject; a first implementation might use existing SAs and delegation as adapters without changing all authentication at once. A prototype should follow one concrete agent action end to end, then cover a Kubernetes API request and one Agentplane service with real policy and UID checks. Inventory direct and delegated consumers, credential sources and RBAC reconciliation before estimating the full migration. Dynamically obtaining and substituting a different Kubernetes SA's token would require a new credential source or broker; the identity plan chose proxy impersonation instead (decided 2026-11-05).

**Policy granularity:** a stable principal with individual external or per-environment accounts does not itself decide whether two environments share grants. Specify whether the principal or each environment is the PolicyBinding subject, what an agent is permitted to do through each attachment, and how its authority is propagated. Avoid accidentally widening privileges by unioning per-environment grants or using an unrestricted environment as a privilege boundary.

### The current implementation makes ServiceAccount lifetime the hard part

Today each Sandbox controller creates a ServiceAccount named after that Sandbox and owns it and its PolicyBinding; deletion removes them. Current admission and authorization deliberately check the exact account UID and its Sandbox controller ownerReference. This is closer to one account _per environment_ than an environment selecting a long-lived shared account. A central Session that survives all environments cannot keep operating as an account deleted with one of them, and two environments cannot currently select one existing ServiceAccount just by updating a field.

The [shared-account Environment provisioner](task_dag.md#environment_shared_account--environments-using-a-named-existing-sa)
can support a verified same-SA caller, but does not settle whether the long-term Session identity is
an SA or an independent Agentplane principal mapped to SAs. That mapping and who may provision or
delegate it remain decisions under `SESSION_IDENTITY_CONTRACT` and `AGENT_PRINCIPAL_MODEL_DESIGN`.
A Sandbox still holds one fixed SA throughout its lifetime in the initial model.

### Long-lived shared ServiceAccount

Manage principal P independently of environment lifetime, authorize environments to run as P, and allow several environments to reference P. This makes namespaced ServiceAccount equality straightforward and uses existing policy subjects, but requires changing Sandbox account and PolicyBinding ownership, token projection, UID checks and managed deletion. Its shared subject also combines grants across environments under current policy semantics. Define revocation, who may bind P and how replacement incarnations are authenticated and fenced.

### Logical principal backed by workload accounts

Give Sessions and environments a durable logical principal while keeping distinct per-environment ServiceAccounts, each explicitly mapped to that principal. Enforce equivalence and allowed delegation at every relevant service; this preserves per-environment token identities but introduces a mapping authority, more policy and a greater drift risk. No additional identity service is assumed.

**Future option:** an Agentplane-owned principal could have independently managed permission bindings
and treat Kubernetes identity as an adapter. The initial implementation uses SAs directly. Revisit
the broader design after accounting for per-agent PolicyBinding granularity and migration of stored
identities and historical attribution. Keep principal distinct from Session UUID, requester, service
broker, workload authentication and journal writer; do not assume an online identity-switch feature.

For a central Session with no environment, the principal still exists and the sampler must exercise authorized capabilities under that principal through an appropriate authenticated delegation or broker path. A stored ServiceAccount name is neither a bearer token nor authority to impersonate it; the central sampler’s own ServiceAccount must not silently become the agent’s. Calls made _inside_ an environment use that environment’s actual Pod/relay and egress identity; verify that this agrees with the Session principal and each destination’s authorization contract. Losing an Environment alone does not imply the agent identity disappeared: the human-operator-only fallback applies when the relevant Sandbox-owned ServiceAccount has actually disappeared, not merely when a Session has no attached Environment. The rights of a surviving independent identity still need their reviewed authorization contract.

**Open cases:** which principal owns a Session when the creator is a human or another agent; who grants acting as P; whether, how and when Session SA associations may change; whether absence is a supported state or only a cardinality limit; recreation and intentional revocation; historical access when P is absent; central Session creation without any environment; multiple exact environments for P; and what is allowed when policies are revoked or an environment is replaced. An Environment's first SA binding cannot be removed or changed in the initial model. A same-name replacement can receive the named principal's grants and Session access under the future contract; preserve actual UID provenance and fence stale effects, and do not treat SA deletion alone as guaranteed durable revocation.

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

Dispatcher cleanup now retains Session rows after required-Environment deletion, as implemented in
[#14314](https://github.com/agentydragon/ducktape/pull/14314). The separate
`SESSION_AND_INBOX_LIFETIME` work remains for Notification inboxes, subscriptions, History handoff,
operator access and explicit Session deletion. Retaining a Dispatcher row does not itself keep an
inbox or harness state alive.

The current direction is Dispatcher-owned Session existence and placement first; a future Session
CRD remains a possible declarative interface or reviewed authority change under
[`DECLARATIVE_SESSIONS_DESIGN`](task_dag.md#declarative_sessions_design--long-running-sessions-and-managed-subscriptions-through-flux),
not a competing writable authority now.

### App archive and environment deletion are different

### App archive

The app stores each Thread’s name and archive flag in its own database. Its archive operation checks for unfinished work; an idle harness can still be running. Archiving is app-level organization, not a Dispatcher deletion, environment deletion, History flush, or proof that the backend Session can no longer accept commands. Unarchive changes app metadata; it cannot by itself restore a deleted harness store.

### Environment deletion

[#14314](https://github.com/agentydragon/ducktape/pull/14314) makes Dispatcher cleanup retain
native and current central V1 Session rows associated with the deleted Sandbox UID. It stores the
exact placement and a required-Environment-deleted marker; `GetSession`, `ListSessions` and exact
`CreateSession` retries expose that state. Cleanup tombstones the old UID, removes dispatch and
ownership rows, and settles materialization separately: `WAITING` becomes `FAILED`, `IN_PROGRESS`
becomes `UNKNOWN`, and accepted or terminal materialization preserves its evidence. A same-name
replacement cannot receive the old Session's work. This does not implement inbox retention or make
central V1 environment-independent. Gateway also hosts Environment Service, but Kubernetes deletion
and a database transaction are separate; reconcile deletion observed through a watch or relist and
fence races with create and submit. Preserve evidence required by each owner's contract.

Existing managed Sandbox teardown quiesces the runtime, waits for History ingress to acknowledge the Session seals, and only then releases its finalizer and state PVC. Dispatcher cleanup should use that established lifecycle rather than inventing an app-archive handoff. The Environment Service issues the UID-fenced deletion; the Sandbox controller manages the actual finalizer. Forced deletion or bypassed finalization can lose unexported data and needs an honest outcome, not a claim that History is complete.

Environment loss leaves the native Session record addressable, but its harness continuation is
unavailable unless the actual store is retained and a supported recovery path exists. A current
central V1 Session record is also retained, with its required executor marked deleted; V1 does not
have detachable or replacement targets. The Session row and the Notification inbox have separate
cleanup owners, so inbox/subscription behavior remains a gap against the selected lifetime direction.

### Decision: Session and inbox lifetimes after environment deletion

The operator asked to resolve Dispatcher Session-record lifetime and Notification inbox lifetime
together. The Notification worker [checks the stored Sandbox name and UID and its owner](../notification_service/worker/destination.go), then [retires an inbox and cancels its subscriptions](../notification_service/store/claims.go)
after confirming a missing or replaced destination. PR #14314 changes Dispatcher cleanup to retain
the Session row, but it does not change Notification's independent cleanup or coordinate the two
stores. An inbox can therefore still retire when its environment disappears.

The selected policy is an independent Session lifetime: deleting a native Session's required
Environment leaves its Session addressable while the native harness is unavailable. History remains
in its separate store and follows its own retention and read policy. PR #14314 implements Dispatcher
Session retention for native and current central V1 placements; it does not implement the selected
inbox/subscription retention. Do not silently bind a replacement or claim native continuation
without its actual state and a separately authorized recovery path.
The intended inbox behavior is to keep subscriptions active and collect authorized events and
messages while delivery is unavailable, but current Notification cleanup still retires stale
destinations. Sustained collection efficiency can be revisited if costs warrant it. Central V1 still
requires its exact executor Environment; retained identity is not environment-independent execution.
Explicit Session deletion, deletion of inbox contents and History, and subscription behavior after
explicit deletion remain separate work. Suspension, an unavailable Runtime, and a deleted or
replaced Sandbox are distinct observations; none proves an in-flight effect failed.

Decide which component owns and observes each transition, how Dispatcher and Notification converge
without a cross-database transaction, and how retries, deletion races, exact-UID fencing,
authorization and History handoff work. The operator's direction for a retained Session whose
Sandbox-owned ServiceAccount has disappeared is human-operator-only read and administration,
through an operator-authenticated entry point such as the app using explicitly privileged service
credentials. The selected future named-principal model allows an authorized same-name replacement to
act as that principal; other workloads are not authorized by the original account's disappearance.
Current UID-pinned access remains in force until the coordinated cutover. Backend maintenance retains
narrowly authorized service operations. Preserve retained messages and History unless their own reviewed policy permits cleanup.
App archive remains separate. Complete forgetting in Dispatcher **when an explicit Session deletion
is implemented** remains the selected identity policy; callers must not deliberately reuse UUIDs,
but Dispatcher has no permanent UUID marker. The separate command-receipt retention question is
deferred, with no period selected.

Track the decision and its scoped implementation gate in [the task DAG](task_dag.md#session_and_inbox_lifetime--decide-session-and-inbox-lifetimes).

**What exists now:** History already retains the associated Sandbox namespace, name and (when known) immutable UID, and its projected `Attached` snapshot includes `SessionSpec` (harness, cwd, model, reasoning effort and instructions). The projection can update model and effort, so it is not necessarily an immutable creation-time spec. This is not a complete Sandbox specification or a record of multiple attachment changes; exact provenance, retention and archive semantics still need design. History is asynchronously fed, so a live reservation does not by itself prove an archival snapshot is durable.

Current sources: [History Service](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/history_service/README.md) · [History summary](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/history_service/history_service.proto) · [SessionSpec and Attached](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/proto/session.proto) · [lifecycle projection](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/history_service/projection/lifecycle.go) · [Gateway and Dispatcher](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/README.md) · [central sampler](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/central_sampler/README.md) · [app Thread archive](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/app/README.md) · [Sandbox deletion and sealing](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/sandbox_controller/SPEC.md) · [required Gateway database URL](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/config.go) · [prior optional-queue proposal in the task DAG](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/task_dag.md#dispatcher_persistence_model_design--choose-the-queues-role-always-on-or-optional-in-one-service).

<a id="opening"></a>

## One CreateSession contract

Give callers the same operation for creating a logical Session on either backend. Choose placement explicitly; let the selected owner prepare, reuse or start its harness as needed.

### Opening behavior in the pre-cutover source snapshot

This table describes the pinned pre-cutover source revision used for the address inventory. Its
wire-field descriptions are historical; the landed [SessionSpec placement contract](../runner_gateway/dispatcher/SPEC.md#sessionspec-placement-cutover)
defines the current contract. Central sampler V1 still requires one exact Sandbox-backed
executor environment, while native store identity and harness-native Session IDs remain future work.

| Layer               | Pre-cutover behavior                                                                                                                                                                                                                               | What success proved                                                                                                                                   |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dispatcher          | Requires a Sandbox name and UID for both harness types, stores an idempotent opening keyed by caller-minted Session ID, and asynchronously delivers it to the selected owner. It reports an opening state.                                         | Dispatcher accepted a durable opening; inspect its state to distinguish queued delivery from owner admission. It does not prove a harness is running. |
| Environment Runtime | Validates the Session, persists its reservation and waits for its local Session owner. Native preparation waits for work; a running harness is not required by this response.                                                                      | The local logical Session was reserved, not that a Claude process, Codex thread or setup completed.                                                   |
| Central sampler     | Currently requires an executor Sandbox and its incarnation, creates durable sampler Session and conversation records, and schedules setup when requested. It may skip executor readiness at Dispatcher delivery but still requires an incarnation. | Sampler state was created or matched; any scheduled setup can still be pending. This is not provider sampling success.                                |

Implementation at the pinned pre-cutover revision: [Dispatcher contract](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/dispatcher/README.md) · [Environment Runtime admission](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/session_admission.go) · [central sampler admission](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/central_sampler/service.go) · [central delivery routing](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/dispatcher/central_runner.go).

### What queued and admitted mean

These describe placement-owner materialization, not the Session's active or archived status and not whether its harness is running. `SESSION_MATERIALIZATION_WAITING` means the Dispatcher has committed the Session and owner delivery is pending; `SESSION_MATERIALIZATION_IN_PROGRESS` means owner delivery is in progress; `SESSION_MATERIALIZATION_ACCEPTED` means the owner durably admitted it; `SESSION_MATERIALIZATION_FAILED` records a proven rejection; and `SESSION_MATERIALIZATION_UNKNOWN` preserves an unconfirmed outcome. `reason`, `delivery_blocker` and `wake_assessment` carry related evidence. None of these values claims harness readiness, a model turn, or History projection.

For native owner admissions, an unconfirmed outcome currently leaves dependent queued commands held as
`session_opening_unconfirmed`; they are not forwarded or used to wake the Environment and remain
cancellable. The Dispatcher contract does not provide automatic resolution for that hold. Whether an
authorized caller or operator can resolve it from durable owner evidence remains open in
[`SESSION_RUNTIME_MODEL_DESIGN`](task_dag.md#session_runtime_model_design--sessions-harness-placement-and-execution-attachments).

Auto-wake is one possible way to unblock a queued environment-hosted opening: when the Sandbox is suspended, enabled and authorized wake can request startup; a denied or disabled wake leaves the opening queued. Queueing can also reflect delivery scheduling, an unready or unavailable owner, retries, or an identity check. The actual current blocker and latest wake assessment are separate from delivery state. If the owner is already available, delivery can proceed without wake. The current central path bypasses execution-Sandbox readiness and wake, but still requires a Sandbox and incarnation at opening; the proposed central placement removes that prerequisite.

The chosen direction is that the Dispatcher Session record establishes the logical reservation. The
owner may materialize it eagerly or when work needs it; opening does not wait for materialization.
Keep delivery and error evidence without multiplying user-facing states or requiring an owner call solely to make a Session exist.

### Proposed common contract

```text
Dispatcher.CreateSession(session_id, spec, setup_script?)
  // SessionSpec.placement is environment_hosted or central_sampler_v1.
→ Session {
    session_id, placement, created_at, updated_at,
    status: { materialization, reason?, delivery_blocker?, wake_assessment? }
  }
Dispatcher.GetSession(session_id) → { session: Session }
Dispatcher.ListSessions(page)     → { sessions: [Session], next_page_token? }
```

The App, Environment Runtime control service and central Sampler name logical creation
`CreateSession` too. App returns the `Session` in its `response_body`; Runtime and Sampler keep
their own owner-side snapshots. The Dispatcher projection intentionally omits immutable creation
instructions and the full spec.

1. **Reserve and route.** The Dispatcher validates identity, authorization, immutable creation request and placement and commits the logical Session. Delivery or materialization at the appropriate owner may be eager or deferred. Exact retries with the same public ID and creation contract return the same Session; conflicting reuse fails. If the response is uncertain, read that exact ID; do not issue a new ID or replay an uncertain native/external effect. Caller-created UUID is not the harness-native ID.
1. **Materialize at the owner.** The environment Runtime or central sampler accepts the logical Session through its own implementation. The Dispatcher row establishes logical existence; no eager owner reservation is required merely to create it. Current delivery is asynchronous and can be queued while an owner is unavailable; never present Dispatcher acceptance as proof the owner has accepted.
1. **Prepare when needed.** Reuse or start a shared Codex app-server, launch a per-Session Claude process, or create central sampler state as appropriate. The caller need not manage those processes. Report relevant progress and failure separately; `CreateSession` does not promise native identity, setup, a ready process or provider output. The owner may perform safe work eagerly, but must preserve the same externally visible acknowledgement and idempotency semantics.
1. **Send a command.** Submitting input is a separate durable operation. Reconcile its admission, execution and any uncertain effects using their own evidence. Do not automatically replay a native create or external action after an unknown result.

### Agreed behavior when the owner is unavailable

Here the owner is the environment Runtime for an environment-hosted Session or the central sampler for a central Session; it is not the individual harness process. After request shape, placement and the applicable authorization have been validated, the Dispatcher database commit establishes logical existence. A successful `CreateSession` does not wait for the owner to be reachable, an environment to wake, History to receive its projection, or a harness-native Session to be created. `SESSION_MATERIALIZATION_ACCEPTED` is later owner admission, not Dispatcher logical commit. The Dispatcher must let callers find the Session and observe its progress independently of History.

Unavailable owners and suspended environments are wait and retry conditions. Materialize the Session
at the correct owner before its work requires it, or earlier if useful; wake an environment only
under an authorized policy, which must support eligible unattended inbox notices.

**Materialization outcomes for the first implementation:**

- **Temporary failure:** retain the logical Session and queued work, and retry when conditions allow, subject to the applicable wake policy.
- **Definitive rejection:** retain the addressable Session and expose the materialization error. Do not automatically retry an unchanged request until relevant conditions change. For a rejected configuration, correct the request by opening a new Session with a new public UUID. The original creation request stays immutable; a future edit-and-retry operation is a separate design.
- **Unknown outcome:** an owner might have accepted even though its reply was lost. Reconcile durable owner evidence and, where admission is idempotent, retry admission with the same public UUID and creation request. Do not infer that an uncertain native creation or external effect is safe to repeat.

This contract leaves the classification of errors, retry triggers, queued-work outcomes on definitive rejection, and how clients observe those facts to implementation design. Retaining rejected Sessions may leave unused records, indefinitely for central Sessions under the current lifetime direction. Runtime-configurable fields and their existing update operations are distinct from rewriting the original creation request.

If the exact required Environment UID is deleted, Dispatcher fences pending creation and delivery,
retains the Session with `required_environment=DELETED`, and settles owner materialization according
to its prior state, as implemented in [#14314](https://github.com/agentydragon/ducktape/pull/14314).
This does not settle the Notification inbox, command-receipt reads, History handoff or explicit
Session-deletion lifecycle; those cross-store behaviors remain open.

**Setup and environment wake are explicit policy questions:** the operator suspects today's
Session `setup_script` belongs to the Environment instead. Decide its owner, possible attachment or
workspace scope, explicit authority, readiness/failure and no-replay behavior under
[`SESSION_ENVIRONMENT_SETUP_DESIGN`](task_dag.md#session_environment_setup_design--choose-where-setup-belongs).
A zero-Environment Session cannot run an Environment script; do not silently select a target or
implicitly repeat setup on later attachments. Opening must not require wake for acknowledgement;
wake ownership and optional eager wake remain open. Native and central setup do not necessarily run
at the same time today.

Where callers need progress, expose it through the existing get/watch mechanisms or an equivalent, with clear Dispatcher-accepted versus owner-accepted meaning; do not require a separate visible phase merely because an implementation has one. Show harness/setup readiness separately only where callers need it; avoid inventing a mandatory separate “start harness” API. A future explicit prepare/warm operation can be added if there is a real use case. Before removing the central Sandbox requirement, choose its independent execution principal, prompt context and optional attachment/setup semantics.

### Environment deletion and explicit Session deletion

Environment deletion no longer removes the Dispatcher Session. [#14314](https://github.com/agentydragon/ducktape/pull/14314)
retains its exact placement and reports `SessionStatus.required_environment=DELETED`; reads and exact
creation retries return the retained record. Cleanup removes dispatch and ownership rows and keeps
the old environment-UID fence, so a same-name replacement cannot adopt the Session. Native state and
central V1 execution are not recovered by retaining this row.

Explicit Session deletion is not implemented. The selected identity policy is that callers use
globally unique Session UUIDs and do not deliberately reuse them; Dispatcher has no permanent UUID
marker or cross-database uniqueness check after a future explicit deletion. History remains a
separate store with its own retention and can reject conflicting attribution, but it is not a
Session-ID lifetime registry. Notification inbox and command-receipt cleanup are separate: #14314
does not preserve inboxes or provide durable reads of command receipts after cleanup. Their lifetime,
authorization and reconciliation remain open under `SESSION_AND_INBOX_LIFETIME` and
`SESSION_DELETION_FENCE`.
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

Review global UUIDs, the agreed `CreateSession` reservation contract, authoritative Session-to-owner
resolution, required versus optional bindings, central authorization scope, and deletion semantics.
Map existing native and central implementations to the same concepts without implying that the
future zero-Environment path has shipped.

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

**Data and rollout.** Preserve existing Session IDs, native stores, history, effective principals and idempotency across any selected transition. The current SessionSpec placement cutover is a coordinated hard cutover with no old Sandbox/runtime compatibility; its migrations preserve Dispatcher Session records, sampler continuation and History evidence in place. Do not use a fresh-state cutover or delete retained Session/history data. The later environment-independent central change also requires migrations preserving central and History data, and must not replay uncertain effects.

**Acceptance examples:** two Sessions in the same environment can reference the same Codex store with distinct native IDs; an unknown native-create result is not replayed; native Session survives ordinary Runtime restart; a shared Codex host can stop without losing Session identities; a central Session can be created and accept provider-only work with zero environments; adding two exact bindings routes each call correctly; deleting one environment does not delete the central Session; an in-flight unknown effect is not replayed; a same-name replacement Environment is not adopted; unauthorized attach/wake/execute is refused; native placement cannot be detached or changed through attachment or generic edit APIs.

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

**CreateSession · What does creation promise?** Agreed: the Dispatcher row creates the logical
Session for both placements without waiting for owner availability; harness startup and native
creation may happen later with the owner. Decide setup timing, optional wake, failure classification
and any readiness/warmup API without requiring an executor for a central Session that does not use
one.

**2 · Are central attachments durable by default?** I recommend durable desired attachments when explicitly added; reachability and active streams stay ephemeral. Ad hoc one-call environment access could remain a separate explicit capability rather than silently creating a lasting attachment. Native placement is a separate required relationship.

**3 · What is the default target?** I recommend explicit attachment selection for central environment tools, with an optional explicit per-Session default. Native-local tools naturally use the required placement environment. Never auto-select an available replacement.

**4 · What survives native environment deletion?** I recommend that historical evidence can remain if its retention and read policy permit it, but native continuation is unavailable unless its actual store and a supported recovery path are retained. Decide whether and how native continuation stores can be archived or transferred; don't infer it from a transcript.

**5 · How much attachment authority is delegated?** Decide the central Session's security scope, who may attach, which capabilities/principal can be selected, and whether revoked policy stops or only prevents new operations. Keep caller identity, broker identity and effective principal distinct.

**6 · What does an agent learn as environments change?** Decide how environment descriptions and skills reach the model, how changes are announced, and which instructions are frozen for native harnesses. A new binding cannot retroactively alter an immutable prompt.

<a id="session-address-inventory"></a>

## Session address inventory and follow-up work

The inventory records the opening lookup and Session inbox diagnostics at its pinned pre-cutover
source revision. It is not the current wire-field contract; [#14248](https://github.com/agentydragon/ducktape/pull/14248)
landed the [SessionSpec placement cutover](../runner_gateway/dispatcher/SPEC.md#sessionspec-placement-cutover),
and [#14314](https://github.com/agentydragon/ducktape/pull/14314) retains Dispatcher Session rows
after required-Environment deletion. Subscription
creation now uses a Session-only request, as merged in [#14103](https://github.com/agentydragon/ducktape/pull/14103).
A Session ID is the public address; an exact environment UID remains meaningful
for native placement, queued-work and execution fencing, authorization, and historical provenance.
An environment-scoped operation or list can still name an environment. Selecting an environment
when creating a native Session is placement, not a compound Session address. No change below
removes History or actual harness state.

| Surface and behavior at the pinned revision                                                                                                                                                                                                                                                                                                                                                                                                                 | Follow-up and owning task                                                                                                                                                                                                                                                                                                                                 |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Pre-cutover Dispatcher API and storage](../runner_gateway/dispatcher/session_dispatcher.proto) required an exact Sandbox on open; current callers use `CreateSession`, `GetSession` and `ListSessions`, with typed `SessionPlacement`. [#14314](https://github.com/agentydragon/ducktape/pull/14314) retains the Session row after Environment deletion and fences its exact UID.                                                                          | Resource and placement cutover are implemented in [#14248](https://github.com/agentydragon/ducktape/pull/14248); remaining [command authority](task_dag.md#delivery_destination_contract--send-commands-to-a-session-id) and durable command-receipt reads remain open.                                                                                   |
| The [browser client](../app/frontend/sandboxes/client.ts) reads openings by global Session ID through the app's operator-authenticated Dispatcher facade. The wire endpoint is [`GET /v1/sessions/{session_id}/opening`](../runner_gateway/dispatcher/session_dispatcher.proto); test callers use [`SessionDispatcherClient.get_session_opening`](../runner_gateway/dispatcher/testing_client.py). App creation still composes presets and Thread metadata. | **Implemented:** the Sandbox-scoped AppService lookup is removed in favor of Dispatcher by Session ID. Review creation separately: a Sandbox can remain a selected native placement without defining Session identity.                                                                                                                                    |
| [App command delivery](../app/frontend/threads/command_delivery.ts) submits new commands by Session ID; the [Session CLI](../cli/sessions.go) likewise uses Session ID for send and interrupt. Previously retained browser commands keep their original Sandbox target.                                                                                                                                                                                     | Preserve the original fence of saved or accepted work and no-replay recovery; remaining broker and authority work is in [Session-ID command addressing](task_dag.md#delivery_destination_contract--send-commands-to-a-session-id).                                                                                                                        |
| [Session Actions](../action_service/session_delegation/contract/inputs.go), their [executor](../action_service/session_delegation/executor.go), and [orchestration](../action_service/orchestration_v1/sessions.go) carry a target environment for opening and Session commands.                                                                                                                                                                            | Review command schema and grant scope in [Session-ID command addressing](task_dag.md#delivery_destination_contract--send-commands-to-a-session-id); retain creation placement and delegated actor/effective-caller checks.                                                                                                                                |
| [Session inbox diagnostics](../notification_service/docs/api.md#session-inbox-diagnostics) use Session ID for `GetInbox` and inbox, entry-summary and subscription watches. Storage enforces one inbox per Session, retains destination keys and cursor/notice state, and refuses conflicting existing inboxes without discarding them.                                                                                                                     | Implemented. Keep current workload ownership and exact Sandbox UID checks; diagnostics do not establish command send or wake authority.                                                                                                                                                                                                                   |
| [Subscription creation](../notification_service/notifications.proto) accepts only `session_id` ([#14103](https://github.com/agentydragon/ducktape/pull/14103)); the [handler](../notification_service/api/subscribe.go) resolves the Dispatcher opening and retains exact Sandbox UID, caller ownership, Runtime membership and source checks.                                                                                                              | Implemented. Sessions without a Dispatcher opening cannot create or resubscribe.                                                                                                                                                                                                                                                                          |
| Notification message sends address a Session ID and resolve its Dispatcher opening before checking a scoped send grant against the exact Sandbox. The [delivery worker](../notification_service/worker/deliver.go) submits by Session ID with an exact-UID authorization fence. Inbox payload reads, acknowledgement and retirement address the Session ID.                                                                                                 | Implemented for current Sandbox-bound Sessions. Preserve exact UID fences, unread messages, idempotency and authorized unattended wake; [central Sessions without Environments](task_dag.md#central_session_notifications--inboxes-for-sessions-without-environments) need separate identity and lifetime work.                                           |
| [Session instructions](../session_instructions/instructions.go) and `AGENTPLANE_SESSION_ID` carry only the public Session ID into native and central harness tools. Central conversation instructions are frozen at admission and reused on resume.                                                                                                                                                                                                         | Implemented: the CLI reads the raw ID and has no reader for the old Sandbox-shaped `AGENTPLANE_DESTINATION` value. Existing central Sessions retain their frozen prompt, which may still show the old Sandbox reference and variable name; use `--session ID` for subscription commands or open a new Session. No prompt or History rewrite is performed. |
| [Central sampler opening](../central_sampler/sampler.proto) embeds a required executor destination and incarnation; central Session storage is separate for conversation/continuation.                                                                                                                                                                                                                                                                      | Session ID remains the Session address; move executor choice to optional attachment/setup under [Session/environment design](task_dag.md#session_runtime_model_design--sessions-harness-placement-and-execution-attachments). Identity and 0..N attachment behavior remain open, not a mechanical rename.                                                 |
| [History reads](../history_service/history_service.proto) already address a Session by ID; [ingress](../history_service/ingress/ingress.go) binds writer, principal and Sandbox provenance, and [FindSessions](../history_service/reads/summaries.go) filters by Sandbox. [Thread views](../app/frontend/threads/client.ts) currently require a Sandbox, while [groups](../app/frontend/threads/thread_groups.ts) organize by it.                           | Keep ID-keyed reads and useful environment filters. Adapt writer authority, optional/multiple provenance and UI in [History and presentation](task_dag.md#session_history_presentation--represent-sessions-without-a-required-sandbox), including eventual attachment transition history.                                                                 |
| [Gateway environment routing](../runner_gateway/gateway/sandboxes.go), [Runtime control](../env/runtime/control/environment_runtime.proto), execution, managed deletion and quiesce are environment-scoped; [system-test Agent](../system_tests/driver/agent.py) and fixtures also carry Session destinations.                                                                                                                                              | Keep exact environment/workload fences for calls to a particular Runtime and environment operations. Update test helpers and assertions with their corresponding address changes; do not replace a safety fence with a UUID-only lookup.                                                                                                                  |

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

This chronology preserves operator choices, reversals and unresolved questions. Current
contracts and implementation status are above; detailed proposals and acceptance gates
remain in the linked plans.

**2026-11-02 · Session identity, placement and continuation.** The operator challenged a
Sandbox-scoped public address and suggested globally unique Session UUIDs, resolved to a
responsible owner and private harness store/native ID. The assistant's earlier
home-scoped recommendation was withdrawn. This is a selected address direction, not
approval of a directory migration or a new owner service. The Codex app-server assigns
the native thread ID in `thread/start` (the upstream
[schema](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/v2/ThreadStartParams.json)
cannot select one), so a lost create response needs durable mapping and evidence-based
recovery. Native Sessions require one environment; central Sessions may eventually have
zero or more optional attachments. Remote native execution and store concurrency remain
unproven.

**2026-11-02 · Live control, retained history and owner availability.** The operator
preferred Dispatcher PostgreSQL for live Session state and a separate History database.
App archive is not backend Session cleanup; the assistant withdrew its proposed archive
handoff. Central Sessions should persist indefinitely for now, with explicit deletion a
low-priority option. Recording attachment transitions in History was desired eventually,
but was a lower-priority correctness improvement. A Dispatcher commit establishes
logical existence even when its Runtime or sampler owner is unavailable; owner
materialization and errors remain separate. A rejected
immutable create can remain visible; corrected configuration needs a new UUID, and
uncertain native effects must not be replayed.

**2026-11-02 · Queue, admission and wake.** With required Dispatcher PostgreSQL, there
is no supported databaseless/queueless mode; immediate-or-fail remains a separate
option. Queue acceptance, owner admission, harness readiness and effects are distinct.
The operator challenged an explicit-resume-only policy, then required that
authorized unattended inbox notices be able to wake an environment. App-owned
resume-and-send was discussed; moving wake orchestration into
Notification was another option. Neither settled wake ownership, caller authorization,
ordinary human-send behavior or cross-database recovery. Existing behavior remains until
a reviewed replacement.

**2026-11-02 · Identity risk; ownership undecided.** Co-resident Sessions share ambient
environment access, so Session IDs or directories alone do not isolate privilege. The
operator said per-Sandbox ServiceAccounts provide individual PolicyBinding subjects;
changing policy bindings can avoid replacing a Pod, but changing its ServiceAccount
cannot. Shared ServiceAccounts combine additive grants, so they change per-Sandbox
permission granularity. External identity, SCIM/OIDC and controller-provisioning
patterns were explored as building blocks, not selected providers or an identity
service.

**2026-11-03 · Address, deletion and authority.** The operator confirmed Session UUID as
the public address and Dispatcher as the live Session database. Session-targeted
commands require a Dispatcher record, including for privileged callers. Explicit Session
deletion means Dispatcher forgets the row; callers must not reuse its globally unique
UUID. No permanent marker, retention period or cross-database uniqueness check was
selected, so an old retry cannot be recognized after forgetting and retained History may
still reject a conflicting writer. On Environment deletion, known-unforwarded work
fails, uncertain in-flight work remains unknown, and accepted work never redirects to a
replacement. The first cutover preserves direct/delegated caller checks, scoped
App/Notification/orchestration trust and separately authorized wake, including unattended
notices. Inbox, receipt, History cleanup and queued-grant revocation remain open.

**2026-11-04 · Environment identity and service ownership.** Sessions sharing an
Environment must use one verified identity because they share ambient access. The
operator favored one fixed ServiceAccount name per Sandbox; an authorized same-name
replacement may act as that named principal while the account remains configured, but its
new UID must fence old work and credentials. Privileged human inspection after account
disappearance does not transfer workload authority. Putting identity in `SessionSpec` was
only a candidate and grants no authority by itself. Environment-independent Session APIs
were reaffirmed; process and
deployment grouping and moving Runtime data between services remained options, not
decisions.

**2026-11-04 · Implemented cutover evidence.**
[#14248](https://github.com/agentydragon/ducktape/pull/14248) landed typed
`SessionPlacement` and the `CreateSession`/`GetSession`/`ListSessions` resource APIs.
Its migrations preserve records, History identity/order and legacy provenance.
[#14314](https://github.com/agentydragon/ducktape/pull/14314) retains Session rows and
exact placement after required-Environment deletion, preserving materialization/effect
evidence and fencing same-name Environment replacements. These changes do not provide
environment-free central Sessions or retain inboxes and command receipts after cleanup.

**2026-11-05 · Workload identity direction; object shapes remain proposed.** The
operator selected SPIFFE workload certificates for Gateway and egress authentication and
Agentplane-owned identities, with no auto-provisioned ServiceAccount per identity. The
operator proposed a shared low-privilege default account available to agents, plus
separate, possibly temporary accounts on request. Two identities may use one account.
Grants attach through separate objects, not account fields, so Flux and runtime writers
can share it. The operator chose separate Agentplane-binding and Kubernetes-grant kinds;
the plan proposes a managed-account object only for accounts Agentplane creates. The
2026-11-04 SA-first direction remains until cutover. Provisioning details,
impersonation versus TokenRequest and rollout implementation remain open in the
[identity plan](identity_and_kubernetes_accounts.md). The operator accepted one
incompatible cutover with downtime, not a staged migration.

**Still open:** directory availability and stale-route fencing; the boundary with
harness owners; command-receipt, inbox and History cleanup/provenance; cross-store
recovery and transition from existing non-UUID native IDs; whether Sessions move or
expose native/store references; and authority for central Sessions without an
Environment. Preserve exact UID fences and retained evidence.

**Maintenance rule:** amend this log when discussion changes the model. Keep approved
decisions, working proposals, reversals and unresolved questions distinct, and update
the main proposal too.

<a id="sources"></a>

## Sources and boundaries

This is a working design grounded in repository `devel` at `12530ca33e` and reconciled against
[#14248](https://github.com/agentydragon/ducktape/pull/14248) and
[#14314](https://github.com/agentydragon/ducktape/pull/14314). Earlier source links and discussion
entries are historical snapshots; the implementation status above describes the checked `devel`
state, not a rollout claim.

- [Existing Session, harness placement and execution attachment design](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/session_runtime_model.md) — scenarios, capabilities, instruction delivery, failure questions.
- [Session/runtime design task](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/task_dag.md#session_runtime_model_design--sessions-harness-placement-and-execution-attachments) and [native identity decision](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/task_dag.md#harness_identity_design--decide-private-session-to-store-and-native-mapping) — current scope and remaining native mapping decisions.
- [Central sampler specification](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/central_sampler/SPEC.md) — current fixed target, lazy executor acquisition, incarnation and recovery limits.
- [Native Session protocol](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/docs/session_protocol.md) · [Environment Runtime](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/SPEC.md) · [Execution specification](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/env/runtime/execution/SPEC.md).
- [Dispatcher contract](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/runner_gateway/dispatcher/README.md) · [multiagent authority and Session access direction](https://github.com/agentydragon/ducktape/blob/d1aeff8ca2/agentplane/plans/multiagent_communication.md).
