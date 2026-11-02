# Caller-privilege Session delegation

Status: **existing-environment tools and scripted acceptance implemented; staging activation approved;
natural-agent usability is an operator follow-up; fresh provisioning deferred.** The remaining
workflow capstone is tracked under
[`THREAD_CREATE_AUTHORIZATION`](task_dag.md#thread_create_authorization--caller-privilege-delegation-capstone).
The direct MCP names are `environments__list`, `environments__get`, and seven `sessions__*`
operations (nine direct tools total) in the Action/MCP contract. The [Session CLI](../docs/agent_cli.md), [Dispatcher
authorization](../runner_gateway/dispatcher/README.md) and [retained History access](../history_service/README.md)
supply the implemented foundations. Shared delegated authentication, persisted Action caller UIDs
and Dispatcher/History receiver enforcement are implemented under the
[delegated authorization contract](../docs/delegated_authorization.md). The external MCP acceptance
is `//agentplane/system_tests/suites/mcp:test_delegation`; it performs real Connection consent and
proves discovery, explicit input, observation and retained reads with the integration app scaled to
zero. Parent CLI acceptance is `//agentplane/system_tests/suites/mcp:test_parent_delegation`.
Both suites prescribe model calls while exercising real interfaces; they do not establish unaided
natural-agent usability. Their generated Sandboxes opt into the direct tools; staging tool exposure
remains separate in [#13514](https://github.com/agentydragon/ducktape/pull/13514).

**Status checked, 2026-11-02 UTC:** guidance, Discovery, caller-delegating Action/MCP adapters,
retained-result projections and both scripted workflow suites have merged. The
[managed guidance rollout](../central_sampler/docs/rollout_acceptance.md#managed-platform-guidance-rollout-2026-11-02-utc)
is verified. The remaining work is natural-agent usability and the staging catalogue/activation,
not another backend or adapter implementation.

## Recommendation and decisions

Start with independent child Sessions in an existing, authorized environment, at the caller's
ServiceAccount authority. Support workload callers and external MCP callers through the same
backend checks. Keep opening a Session and submitting its first input separate. Provide a small
observation tool that correlates a command with its turn and returns structured facts.

| Contract item      | Selected behavior                                                                                                           | Consequence / alternative                                                                                                                                                                   |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| First target scope | Reuse an existing environment with the same SA UID as the caller                                                            | New-environment creation and its template catalogue are deferred until catalogue ownership/location is selected; they do not hold existing-environment delegation.                          |
| Session authority  | Record the logical execution principal at Dispatcher admission, separately from backend peers and evidence writers          | Deriving authority only from a live environment makes retained reads and future environment-free Sessions depend on that environment's lifetime.                                            |
| Agent interface    | Backend-authorized Session tools through MCP and CLI                                                                        | An Action wrapper is also possible, but needs verified caller attribution and the same backend enforcement. Existing operator-approved orchestration remains a distinct authority path.     |
| Approval defaults  | Routine same-principal Session operations execute automatically; new-environment creation initially requires human approval | Dedicated environment-management Actions need their own typed provisioning contract. `orchestration_v1` is not a prerequisite for either new path.                                          |
| Child results      | Observe input delivery and the correlated turn's output; follow up with another input                                       | A task-level `report`/`blocked` protocol is additional product work. Neither turn completion nor inbox acknowledgement proves task success.                                                 |
| Required reads     | A narrow same-principal read/control contract for this workflow                                                             | Approved for this same-principal workflow; broader cross-SA archive policy remains held. Without result reads, select another authenticated result-delivery contract before implementation. |

The stakeholder approved this contract. The DAG records remaining integration outcomes and their
prerequisites. Reliability remains P0; broader authorization and fresh provisioning retain their
separate decision gates.

### Accepted choices, 2026-10-31

- **Full template catalogue, when implemented:** return the complete deployment-published catalogue,
  with pagination and no per-SA template filtering. Catalogue ownership and fresh provisioning
  are deferred; the first release reuses existing environments.
- **Additive briefing:** `sessions__open.additional_instructions` appends a child briefing while
  preserving the default platform guidance. It does not replace the effective prompt.
- **Explicit first input:** opening a Session, including with `additional_instructions`, does not
  start a model turn. The caller must submit input separately to start work.
- **Future creation identity:** reuse the caller's existing SA and permissions without owning or
  changing that SA, adding PolicyBindings or manufacturing grants. Request extra access separately.
- **Future template lifetime:** pin the approved configuration across restarts; updating it is an
  explicit operation. Where the published catalogue lives and how its revisions are owned still
  need review before creation is implemented.

## Authorization and approval defaults

**Approved implementation direction, 2026-10-31:** avoid depending on
`orchestration_v1`; extract environment management into dedicated Actions. Human approval for
creating an environment is the initial default.

Use direct backend gRPC and CLI access where the existing APIs make that straightforward.
Action adapters are also approved as an initial surface. Routine same-principal operations do not
wait for a human Decision. An Action adapter's automatic execution must use
the same verified caller and backend checks; an Action wrapper cannot substitute its own broader
service authority.

**Approved delegated-authorization protocol, 2026-10-31:** an Action adapter authenticates as
Actions and carries the original caller's pinned SA identity in the shared
[delegation envelope](../docs/delegated_authorization.md). The backend checks the actor's explicit
permission to delegate and applies normal authorization to the effective caller, with no privileged
fallback. Reuse the implemented shared protocol and durable Action caller UID. Dispatcher,
History and Discovery explicitly admit delegated calls on configured methods. Direct clients retain
caller authentication.

The [tool reference](#tool-contract-derived-from-the-workflows) specifies the authorization and
approval rule for every proposed operation.

The narrow same-principal read contract is approved; reads need no per-call human approval.
An external MCP caller uses its Connection's verified account under the same rules. Normal access
requires a verified SA in the served namespaces and per-object UID checks, not a second per-SA
Dispatcher caller allowlist. Existing privileged backend bypass remains explicit and Pod-bound. Neither a
parent/creator label nor a discovered handle confers access. Failed authorization is a structured
denial; privileged broker operations require a separate explicit contract. Runtime validation
and configured model/resource policies still apply to otherwise authorized calls.

Creating a new environment is a separate Action with human approval initially. Its request must
show the concrete template/backend, resources, execution account and workload mounts/security settings;
approval covers those parameters, not arbitrary future Session control or a different account.
Other environment lifecycle Actions need their own reviewed scope and approval policy. This
proposal adds no blanket auto-approval rule for environment management.

## First implementation scope

The implemented scope is existing-environment list/get and the seven Session operations. An external
MCP caller can reuse an eligible existing environment without having an environment of its own. An empty list
means an operator must prepare a suitable environment; the first release does not create one.
`environments__list_templates`, `environments__get_creation` and `environments/create` remain
later design/implementation work. Keep their sketches below to preserve the agreed direction,
without making catalogue decisions prerequisites for the existing-environment workflow.

## Transport and implementation mapping

**Operator direction, 2026-10-31:** aim for an approximately one-to-one mapping between domain
operations and owning gRPC methods, callable from the Agentplane CLI. Use the existing gRPC APIs
now where little additional work is needed. Actions-first is acceptable elsewhere; keep the
verified caller and backend ownership, and record each remaining RPC/CLI extraction explicitly.
This does not require a new service or binary, a general transport rewrite, or `orchestration_v1`.

| Operation                                                 | Backend mapping                                                                            | Implemented surface / deferred work                                                                                                                                                                                                                 |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Open, submit input, cancel queued input, interrupt a turn | Existing `SessionDispatcher.OpenSession`, `SubmitInput`, `CancelDispatch`, `InterruptTurn` | Implemented through `sessions__open`, `sessions__submit_input`, `sessions__cancel_input`, and `sessions__interrupt_turn`; CLI also exposes owning Dispatcher calls. Additional briefing is additive; staging direct-tool exposure remains separate. |
| Get Session                                               | `SessionDispatcher.GetSessionOpening` plus `HistoryService.GetSessionSummary`              | `session_client.GetSession` composes both sources without a live-environment lookup; exposed through `sessions__get` and `agentplane sessions get`. No new owning snapshot RPC is needed now.                                                       |
| Read turns, observe one input                             | Existing History reads/follows plus Dispatcher command receipts                            | `session_client.ObserveInput` and `ReadTurns` preserve source facts and cursor coverage; exposed through `sessions__observe_input`, `sessions__read_turns`, and `agentplane sessions observe-input/read-turns`. No new owning RPC is needed now.    |
| List/get existing environments                            | `EnvironmentDiscovery` over Sandbox inventory and shared runtime/configuration sources     | Exposed through `environments__list` and `environments__get`. The Action route carries the original caller to Discovery; it never queries under Actions' broad SA or returns an unfiltered namespace list.                                          |
| List templates                                            | Full deployment-published catalogue                                                        | Extract shared defaults from app-owned presets. Direct backend read can share the Action Service process; no app calls. TODO: dedicated read RPC/CLI if initially exposed as an Action.                                                             |
| Create environment, observe creation                      | Dedicated `environments/create` Action plus neutral provisioning/lookup code               | Deferred: implement shared-account environments and the reviewed catalogue. Reuse persisted caller UID. TODO: typed create/lookup RPCs and CLI access for initially Action-only operations; direct create still needs approval authority.           |

### Budget and broader-policy boundaries

Count limits do not replace spend controls: the operator's stated concern is provider usage cost.
A future per-account spend budget belongs at LLM ingress and also covers message-triggered work.
No new spend-budget implementation or general different-privilege delegation policy gates this
caller-privilege slice. Broader launch/default/grant and cross-SA archive decisions remain in
their existing DAG nodes.

## What an environment means in this version

An execution target is an existing managed `Sandbox`, Pod or VM, identified by Kubernetes
`namespace`, `name` and immutable object `uid`. The tool calls it `target`; it does not introduce a
new environment object or Session-attachment entity. That UID distinguishes a deleted/recreated
Sandbox. Pod UID, VM instance, container restart and executor generation remain separate facts.

The caller may have no environment and no Agentplane Session: an external MCP connection is
sufficient authentication when its bound account is authorized. The child still needs one target
in this version. Native harnesses and the central sampler's current Session API require a target;
central Sessions pin one Sandbox UID, with executor acquisition lazy when needed. Zero-environment
children and 0..N attachments remain the [runtime north star](session_runtime_model.md), not a
claimed feature of this slice.

Sharing a target and giving each child a working directory matches the stakeholder's current
workflow. It creates no security isolation between those Sessions. Parentage is optional provenance
and confers no permission. No parent transcript is copied automatically; the creator supplies the
child's additional standing instructions and initial task; platform guidance is supplied separately.

## Instruction composition

**Accepted interface choice, 2026-10-31:** the spawning tool accepts `additional_instructions`,
separate from `spec`. It is an additive child briefing, not the child's entire effective prompt.
Omitting it or supplying an empty string never removes Agentplane platform guidance.

The published input schema must describe `additional_instructions` as: "Standing instructions
added to the Session. This field does not submit input or start a model turn. After opening,
call `sessions__submit_input` with the task to start work." The `sessions__open` description must
state the same activation rule; it must not turn the briefing into an implicit initial input.
Optional setup may execute shell commands during opening, separately from any model turn.

Keep these sources distinct:

- **Harness base instructions:** retain the native harness's normal base prompt, or the central
  sampler's own equivalent. Agentplane orientation must not replace that base prompt.
- **Platform guidance:** deployment-owned Agentplane orientation, tool/API discovery and recovery
  guidance, included by default independently of optional launch presets. Render it for the
  actual harness/tool surface; a central loop or MCP client must not be told that it runs inside
  the target environment or has local tools it lacks.
- **Environment context:** the selected target's description and instructions, with the child's
  own identity/notification destination where supported. Never copy the parent's destination.
  This describes the environment and does not establish new execution permissions.
- **Role/preset instructions, when selected:** optional role-specific text such as public-coder
  contribution practices. A caller's additional briefing appends to the selected role text.
  Generic Agentplane guidance currently bundled with a preset belongs in the platform layer.
- **Additional instructions:** the creator's child-specific standing brief. The first task is
  still submitted separately through `sessions__submit_input`.

The first tools need not accept preset names or introduce a backend preset registry. A client
using an optional role preset combines its concrete role text and child briefing in
`additional_instructions`. The backend adds the platform/environment layers regardless of whether
that client uses a preset or the integration app.
A child does not implicitly inherit the parent's role, private instructions or transcript.

The [native Harness Host](../env/runtime/harnesshost/admission.go) and configured
[Central Sampler](../central_sampler/store.go) use the shared
[Session instructions composer](../session_instructions/README.md) for platform guidance, the
child's destination and additive instructions. The native
[configuration](../env/configuration/configuration.go) requires nonempty platform instructions;
managed central configuration now supplies them as well. Preset text remains caller-supplied.
Central provider base prompts remain separate from the Agentplane platform guide. Consolidating
which component owns guidance configuration/composition is a
[later design](task_dag.md#platform_guidance_ownership_design--decide-platform-guidance-ownership-and-composition).

Resolve the applicable guidance once when a new Session opening is durably admitted, retaining
its effective content and source provenance for execution and inspection. Native and central
paths share the same composition semantics through neutral code/configuration; no new service,
app dependency, parent Session or live executor is required to supply platform guidance.
Repeated opening of the same UUID/request recovers that resolved snapshot instead of appending
again or resolving against a newly edited deployment prompt. Resume/rollover retains it; changed
default guidance applies to newly opened Sessions. Retaining that prompt does not freeze live
permissions, tool availability or environment state; those remain discoverable runtime facts.

A deliberate omission/replacement of platform guidance should require a separate, explicit
advanced option, never an empty `instructions` value or ordinary additional briefing. This draft
selects additive behavior for the normal spawning tool; an advanced override needs its own
reviewed interface when needed. Prompts are not an authorization boundary: changing or
omitting guidance does not change backend RBAC, Action policy or environment isolation.

## Proposed workflow A: agent reuses its environment

Platform instructions describe tool/API discovery for the selected runtime and supply the child's
notification address. Native runtime configuration also puts that address in
`AGENTPLANE_DESTINATION`. Those identifiers are addresses, not credentials; Discovery verifies
eligibility against the authenticated caller. Direct-tool availability depends on the target's
configured catalogue and must be discovered rather than assumed from the guide.

The example is a CI-maintenance coordinator delegating investigation of one latency regression.
The direct MCP contract uses `<group>__<action>`, such as `environments__list` and
`sessions__open`; their schemas are available through MCP `tools/list`. The CLI exposes the same
Action schemas and calls through `agentplane --json actions show environments/list` and
`agentplane --json actions submit environments/list --title ... --arguments JSON --wait`. The
`agentplane sessions` CLI group also exposes the owning Dispatcher/History APIs. Today's central
code mode can reach a CLI through `exec`, but has no `sessions` JavaScript global. A scripted MCP
client verifies protocol behavior; it does not establish that a natural model can discover or use
these tools unaided.

1. Discover tool schemas through MCP `tools/list`, or inspect the Action schema with
   `agentplane --json actions show environments/list`. Read `environments__list` and call:

   ```json
   { "limit": 20 }
   ```

   The response contains only targets the authenticated account may use. One illustrative entry:

   ```json
   {
     "target": {
       "namespace": "agentplane",
       "name": "ci-worker",
       "uid": "51b74ff8-9a36-4b55-a9f5-54806077782a"
     },
     "execution_account": {
       "namespace": "agentplane",
       "name": "ci-worker",
       "uid": "569f9631-0068-4122-8005-0ae9cf2c7056"
     },
     "readiness": { "state": "ready" },
     "workspace": { "root": "/workspace" },
     "instructions": "Shared CI workspace; give each Session its own directory.",
     "configurations": [
       {
         "harness": "HARNESS_CODEX",
         "model": "offered-model",
         "reasoning_efforts": ["low", "high"]
       }
     ],
     "observed_at": "2026-10-31T18:00:00Z"
   }
   ```

   `offered-model` denotes the actual model ID returned by discovery, not a literal to send or a
   model guessed from these docs. This whole descriptor is proposed. Configuration must come from
   the owning runtime or a shared deployment catalog checked against that runtime. Launch presets
   alone cannot establish what an existing target supports. Discovery includes freshness and
   pagination; readiness/configuration may change before admission. `cwd` is an absolute path.
   Native Runner also rejects paths outside or equal to its workspace root when one is configured;
   the current central sampler does not apply that root restriction. These are existing backend
   checks, not a new selectable `cwd_policy`. Returned workspace information and environment
   instructions guide directory selection; admission remains authoritative.

2. Match the full target reference against the agent's supplied destination and choose an offered
   harness/model/effort. Inspect the existing repository with ordinary execution tools. Prepare a
   distinct directory or Git worktree under the returned workspace root, for example
   `/workspace/ci-latency-worker`. Directory preparation is an explicit command, not a hidden copy
   of the parent's repository. Runtimes already create missing cwd directories; repository setup
   needs explicit commands. A client without execution access can provide `setup_script` to
   `sessions__open`; setup completion is observable separately from opening.

3. Mint and retain a child Session UUID **before** sending. Call `sessions__open`:

   ```json
   {
     "target": { "namespace": "agentplane", "name": "ci-worker", "uid": "51b74ff8-9a36-4b55-a9f5-54806077782a" },
     "session_id": "d71434fa-f5ba-4fb4-b152-e31bd4b37c59",
     "spec": {
       "harness": "HARNESS_CODEX",
       "model": "offered-model",
       "reasoning_effort": "high",
       "cwd": "/workspace/ci-latency-worker"
     },
     "additional_instructions": "Investigate delegated CI latency work. Use this directory; state uncertainty and link evidence."
   }
   ```

   Substitute the model discovered in step 1. The result contains the existing Dispatcher opening
   record, initially `DELIVERY_STATE_QUEUED_IN_DISPATCHER`. It proves durable admission of an opening,
   not that a model has accepted work. The target may be starting or suspended. An authorized wake
   uses the current wake policy; no promise that every suspended environment wakes automatically.
   Opening with this briefing does not start a model turn. Send the first task in step 4.

4. Mint and retain a command UUID; call `sessions__submit_input` separately:

   ```json
   {
     "target": { "namespace": "agentplane", "name": "ci-worker", "uid": "51b74ff8-9a36-4b55-a9f5-54806077782a" },
     "session_id": "d71434fa-f5ba-4fb4-b152-e31bd4b37c59",
     "command_id": "173c7199-ab88-4237-8553-831806e62c3c",
     "text": "Find the cause of the last 24 hours' CI latency increase. Compare against the preceding day; propose a fix with evidence. Do not open a PR yet."
   }
   ```

   The result contains a Dispatcher `dispatch`. Input can queue behind the opening: the caller
   need not poll for a History row first. Successful submission says the command is stored, not
   consumed. The child gets a normal harness input and its usual execution tools. In current
   central code mode, Actions are available through CLI execution, not direct nested Action tools.
   Execution uses the target's authority, which this first slice requires to match the caller.

5. Continue independent work, then call `sessions__observe_input`:

   ```json
   {
     "session_id": "d71434fa-f5ba-4fb4-b152-e31bd4b37c59",
     "command_id": "173c7199-ab88-4237-8553-831806e62c3c",
     "wait_seconds": 30
   }
   ```

   An example successful result:

   ```json
   {
     "session_id": "d71434fa-f5ba-4fb4-b152-e31bd4b37c59",
     "command_id": "173c7199-ab88-4237-8553-831806e62c3c",
     "dispatch": {
       "kind": "observed",
       "source": "dispatcher",
       "observed_at": "2026-10-31T18:05:00Z",
       "value": {
         "session_id": "d71434fa-f5ba-4fb4-b152-e31bd4b37c59",
         "command_id": "173c7199-ab88-4237-8553-831806e62c3c",
         "sandbox": "ci-worker",
         "sandbox_uid": "51b74ff8-9a36-4b55-a9f5-54806077782a",
         "operation": "OPERATION_SUBMIT_INPUT",
         "state": "DELIVERY_STATE_RUNNER_ADMITTED",
         "created_at": "2026-10-31T18:01:00Z",
         "updated_at": "2026-10-31T18:01:01Z"
       }
     },
     "observation": {
       "kind": "turn_completed",
       "source": "history",
       "gaps": [],
       "resume_token": "opaque-observation-cursor",
       "turn": {
         "turn_id": "observed-harness-turn-id",
         "input_command_ids": ["173c7199-ab88-4237-8553-831806e62c3c"],
         "status": "TURN_STATUS_COMPLETED",
         "final_assistant_message": "The queue delay increased after ..."
       }
     }
   }
   ```

   The envelope is structured; the final assistant message is model-authored content, currently a
   string. If the coordinator requests JSON, it must parse and validate that content itself. No
   backend `accepted_task`, `reported` or `blocked_task` state is inferred. The coordinator can
   send a follow-up with a fresh command UUID, keep using the child as a standing worker, or stop
   a particular turn. Ending a turn does not delete or terminate the Session.

After discovery/preparation, the ordinary sequence is **open → submit → observe**. Follow-up work
is **submit → observe**. A bounded observation timeout returns facts so far and a cursor; it does
not cancel work. Call again with that cursor or end the coordinator's turn if an explicitly
supported notification route will wake it. This proposal does not invent a Session-completion
subscription.

## Proposed workflow B: external MCP-only coordinator

The coordinator has an authenticated OAuth connection to Agentplane MCP; it has no kubeconfig,
execution host, parent Session or notification inbox. The acceptance is runnable with
`bbr test //agentplane/system_tests/suites/mcp:test_delegation`; it uses the MCP SDK and real
Connection consent, then scales the integration app to zero before any delegation Action call.

1. `tools/list` exposes the opted-in direct tools. Call `environments__list({"limit":20})`;
   it evaluates the connection's verified bound SA, rather than asking the model to provide one.
   With an eligible target, use the full reference and offered configuration exactly as in workflow A.
2. Call `environments__get` with the exact discovered target, then `sessions__open` with a retained UUID and usable absolute cwd, respecting the backend's
   existing path validation. The runtime creates a missing directory. Repository preparation can
   use an explicit `setup_script`, with its result visible through `sessions__get`; a clone needs
   an explicit source and authorized credentials. Opening does not implicitly copy another repo.
3. Call `sessions__submit_input`, retain the command UUID, and use `sessions__observe_input` to read
   the correlated result. The external caller can reconnect and resume reads with those IDs.
   Call `sessions__get` for the independently sourced opening and retained History summary, then
   page `sessions__read_turns` with its Session-scoped `page_cursor`. Returning the result does not
   require delivering a message into the external harness.

An account can legitimately receive an empty target list. The acceptance fixture creates a
Sandbox-owned ServiceAccount, marks that exact account eligible for MCP token minting, and completes
operator Connection consent for it; the chart does not precreate the account. This proves the
same-SA contract in the disposable fixture. Staging direct-tool activation remains separate in
[#13514](https://github.com/agentydragon/ducktape/pull/13514); the managed-guidance rollout is complete.

### Later: when no eligible environment exists, dedicated provisioning Actions

This path is deferred at the stakeholder's request until template catalogue ownership/location is
settled. Today underlying Pod/VM workload templates are namespaced Kubernetes `SandboxTemplate`
objects generated from Git-managed cdk8s configuration; launch defaults also live in app/deployment
presets. The fuller backend catalogue needs an explicit owner. The sketch uses dedicated tools
and an Action over typed provisioning code, with no `orchestration_v1` dependency.

1. Discover the later `environments__list_templates` proposal through `tools/list` and call `{"limit":20}`. Select a
   deployment-published VM or Pod configuration and retain its complete `template` reference, including its
   revision. The returned configuration includes the caller's actual execution account, resources,
   mounts and workload security settings. Publication means the configuration is offered for submission, not that
   the caller is approved to create it. No new per-SA template-eligibility rule is proposed.
   The agent does not guess template names, revisions or resource defaults. Discover `environments/create` and its current schema/policy through the normal Action catalog.
2. Retain a creation UUID and a separate Action key before submitting. For example, after discovering
   the illustrative `ci-vm` template reference below, call the existing `request_action` tool:

   ```json
   {
     "request": {
       "idempotency_key": "ci-vm-creation-2026-10-31-01",
       "action": { "group": "environments", "name": "create" },
       "arguments": {
         "creation_id": "0698a0d2-fb0d-4c84-a17e-e7c85f5c57a4",
         "name": "ci-worker-vm",
         "template": {
           "namespace": "agentplane",
           "name": "ci-vm",
           "uid": "4ab7e198-02c4-472f-8b5d-61d533760229",
           "revision": "opaque-discovered-configuration-revision"
         }
       },
       "title": "Create the reviewed CI VM environment"
     },
     "wait": { "wait_seconds": 30, "wait_until": "terminal" },
     "respond_with": "result"
   }
   ```

   The template reference is copied from discovery, not invented from this example. Human approval
   is the initial creation policy. Use the generic request tool: existing direct Action tools only
   execute auto-approved calls; they do not enter the human approval path.

3. Pending approval, denial, allowed Decision and Execution outcome are separate Action facts. If
   the reply is lost or the bounded wait expires, use `get_action_request` with the retained
   `idempotency_key`, then its ordinary result lookup. Do not resubmit under a new key; today's
   Action API refuses repeated submissions of the same key too.
4. Call the later `environments__get_creation` proposal with `{"creation_id":"0698a0d2-fb0d-4c84-a17e-e7c85f5c57a4"}`
   to observe the backend independently of Action polling. `not_observed` is expected before
   backend admission and does not prove a timed-out request had no effect. `provisioning` proves
   admission; `created` returns the full `EnvironmentDescriptor`, including the new Sandbox UID.
   `unknown` remains uncertain while the backend reconciles; no new creation is implied.
5. Refresh that exact target with `environments__get`, select its offered runtime configuration,
   then use **open → submit → observe** from workflow A. Opening can queue before readiness. An
   approved creation does not itself open a Session, guarantee readiness or grant a different
   principal authority.

Later same-SA provisioning needs controller support for a borrowed account with pinned UID,
a revision/snapshot contract and typed creation recovery. Those are real backend gaps, covered by
[`ENVIRONMENT_MANAGEMENT`](task_dag.md#environment_management--dedicated-environment-tools-and-creation).
These later tool/schema sketches remain below. Existing-target discovery and delegation
can ship independently of fresh provisioning; an external caller with no eligible target must
wait for provisioning support or operator preparation of a suitable environment.

The existing [`orchestration_v1` broker](../action_service/orchestration_v1/README.md) creates a new
SA and remains a separate legacy capability. Its system scenario
`launcher_drives_only_the_sandbox_it_launched` proves that brokered flow, not this new interface.

## Tool contract derived from the workflows

The first nine rows are the accepted direct MCP tools and typed Action contracts; the CLI exposes
the same Action schemas through `agentplane --json actions show GROUP/ACTION` and
`agentplane --json actions submit GROUP/ACTION`. The remaining provisioning rows are deferred
sketches. Direct tool names are `<group>__<action>`; Action names use `group/action`. Results use MCP
`structuredContent` plus a matching JSON text fallback. Callers do not parse prose for state,
authority failures, IDs or recovery. The CLI exposes equivalent machine-readable output.

The environment reads and Session operations enforce authority at their owning backends, whether
exposed directly or through an initially Action-based adapter.
**`environments/create` names an Action** (`group: "environments", name: "create"`), submitted
through the existing generic `request_action` envelope; the slash is not an MCP tool name. Its
inner argument/result schemas appear in the same reference table below. An Action executor must
preserve the verified requester's identity when calling the typed provisioning backend.

### Schema notation

These are compact record schemas for review, not executable TypeScript. `integer` means a JSON
integer; `UUID` means a UUID-format string; opaque identifiers/cursors are nonempty strings.
Inputs reject unknown keys. Optional fields are omitted when absent. Implementation publishes
JSON Schema `inputSchema`/`outputSchema` and equivalent backend protobuf contracts from the owning
types; the linked existing protobufs remain authoritative for reused messages and enum values.

`Dispatcher` below is [session_dispatcher.proto](../runner_gateway/dispatcher/session_dispatcher.proto),
`History` is [history_service.proto](../history_service/history_service.proto), and `Harness` /
`TurnStatus` are the [existing Runner enum](../proto/session.proto) / [Event enums](../proto/event.proto).
Examples use protobuf field names; JSON Schema must pin that field spelling explicitly. New
projections belong to neutral shared clients/backends, with no `orchestration_v1` DTO dependency.

| MCP tool / Action                        | Purpose                                                                                                                                                                                                                                                                                                           | Approval / authorization                                                                                                                                                                          | Input schema                                                                                                                          | Success-output schema                                                                                                      |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **Later:** `environments/list_templates` | Discover the full deployment-published Pod/VM catalogue, exact revision handles and what a creation approval would cover. Use when no eligible environment exists; it creates nothing.                                                                                                                            | Authenticated read of the full catalogue, without per-SA template filtering; discovery is not creation approval.                                                                                  | `{limit: integer (1..100), after?: OpaqueCursor}`                                                                                     | `Page<TemplateDescriptor>`                                                                                                 |
| `environments__list`                     | Discover existing environments eligible for Session work, including their full handles, readiness, instructions and runtime choices. An empty list is valid; a backend failure is an error.                                                                                                                       | Automatic, filtered to the caller's eligible same-SA targets.                                                                                                                                     | `{limit: integer (1..100), after?: OpaqueCursor}`                                                                                     | `Page<EnvironmentDescriptor>`                                                                                              |
| `environments__get`                      | Refresh one environment's state and usable Session configuration. Use the complete handle; a same-name replacement is refused.                                                                                                                                                                                    | Automatic for an authorized target.                                                                                                                                                               | `{target: TargetRef}`                                                                                                                 | `EnvironmentDescriptor`                                                                                                    |
| **Later:** `environments/get_creation`   | Look up a creation by its retained ID after a lost reply or uncertain outcome. This read neither repeats creation nor approves a pending Action.                                                                                                                                                                  | Automatic for the creation's authenticated owner.                                                                                                                                                 | `{creation_id: UUID}`                                                                                                                 | `CreationObservation`                                                                                                      |
| **Later Action** `environments/create`   | Create a managed environment under the caller's account from the reviewed template revision. Return a creation receipt; it does not open a Session or promise readiness.                                                                                                                                          | Human approval initially, plus backend validation of the actual caller/account, namespace, revision and configuration.                                                                            | `{creation_id: UUID, name: string (Kubernetes DNS subdomain, 1..253), template: TemplateRef}`                                         | `CreationObservation` as the Action's result, inside its ordinary Action request/Decision/Execution envelope.              |
| `sessions__open`                         | Durably open a child Session with default platform guidance, an optional additive brief and one-time setup. Opening does not start a model turn; send the task with `sessions__submit_input`. Same UUID/request recovers the original; conflicting reuse fails. Admission does not mean harness/model acceptance. | Caller is authenticated; target's actual SA UID equals caller's verified SA UID; target UID is fenced at Dispatcher admission; the owning runtime validates the requested harness/model settings. | `{target: TargetRef, session_id: UUID, spec: SessionSpec, additional_instructions?: string, setup_script?: string (maxLength 65536)}` | `{opening: Dispatcher.SessionOpening}`; normally queued initially.                                                         |
| `sessions__submit_input`                 | Durably admit input separately from opening; same command ID/request recovers its original admission.                                                                                                                                                                                                             | Same trusted logical Session principal and actual target execution account as caller; target/Session binding and target UID match.                                                                | `{target: TargetRef, session_id: UUID, command_id: UUID, text: string (nonempty)}`                                                    | `{dispatch: Dispatcher.Dispatch}`; stored is not consumed.                                                                 |
| `sessions__observe_input`                | Bounded, read-only observation correlated to one command; timeout does not cancel work.                                                                                                                                                                                                                           | Same retained logical principal under the narrow read rule; live environment not required.                                                                                                        | `{session_id: UUID, command_id: UUID, wait_seconds: integer (0..30), resume_token?: OpaqueToken}`                                     | `{session_id: UUID, command_id: UUID, dispatch: SourcePart<Dispatcher.Dispatch, "dispatcher">, observation: Observation}`. |
| `sessions__get`                          | Read opening plus retained History snapshot, including setup/harness state and active turn when observed.                                                                                                                                                                                                         | Same retained logical principal under the narrow read rule; live environment not required.                                                                                                        | `{session_id: UUID}`                                                                                                                  | `SessionSnapshot`; missing History remains distinct from a missing Dispatcher opening.                                     |
| `sessions__read_turns`                   | Page through compact retained turns; debug/evidence reads remain separate.                                                                                                                                                                                                                                        | Same retained logical principal under the narrow read rule; live environment not required.                                                                                                        | `{session_id: UUID, count: integer (1..100), page_cursor?: OpaqueCursor}`                                                             | `TurnPage`.                                                                                                                |
| `sessions__cancel_input`                 | Cancel only an unclaimed queued dispatch.                                                                                                                                                                                                                                                                         | Same control authority; named command must belong to the Session.                                                                                                                                 | `{session_id: UUID, command_id: UUID}`                                                                                                | `{dispatch: Dispatcher.Dispatch}`.                                                                                         |
| `sessions__interrupt_turn`               | Interrupt exactly the observed turn; never targets a successor turn.                                                                                                                                                                                                                                              | Same control authority; Session, target incarnation and observed turn must match; use a fresh interrupt command UUID; runtime must be reachable.                                                  | `{target: TargetRef, session_id: UUID, command_id: UUID, turn_id: OpaqueId}`                                                          | `Dispatcher.InterruptTurnResponse`; includes existing state/reason/admission.                                              |

### Environment records and creation semantics

The accepted discovery contract exposes the full deployment-published catalogue through its
owning backend, without per-SA template filtering. Catalogue reads expose published configurations; ordinary Action schema and
policy/Decision processing governs a creation request. Initially creation requires human approval.
Any future per-template request restriction needs an explicit policy owner and enforcement
contract; it must not be inferred from catalogue membership or a preset.

The later creation schema borrows the caller's existing SA and live permissions; it never adds
PolicyBindings or grants and does not pin authorization state. It selects a complete workload
configuration for review; it has no arbitrary Pod
spec, alternate-account or resource-override field. The reviewed configuration fixes the namespace;
`name` must be a valid Sandbox object name there. `TemplateDescriptor.configuration` shows the
selected Pod/VM resources, mounts, workload security settings and bootstrap before submission. The trusted approval
view must resolve the exact requested revision; a missing/stale revision cannot silently resolve
to a newer one. Execution must materialize that approved configuration or refuse before effects.
Mutable templates currently resolve again on every start, so this revision/snapshot contract is
new controller/provisioner work. Later changes/restarts need their own lifecycle contract.

The backend binds `creation_id` to caller UID and the exact request before provisioning. An exact
backend retry resolves the original receipt; conflicting reuse is refused. An unknown outcome remains
unknown until reconciliation establishes what exists. The original Action key separately recovers
approval/execution state through `get_action_request` or `get_action_result`. No general queue,
new template CRD or storage implementation is selected by these contracts.

Stopping, deleting, resizing and editing an environment are outside this first interface. Their
future schemas and approval policies must account for every Session sharing that environment.

```typescript
type UUID = string; // UUID format for Session, command and creation IDs
type OpaqueId = string; // nonempty; no UUID or ordering assumption
type OpaqueCursor = string;
type OpaqueToken = string;
type Timestamp = string; // RFC 3339
type AccountRef = { namespace: string; name: string; uid: OpaqueId };
type TargetRef = { namespace: string; name: string; uid: OpaqueId };
type TemplateRef = {
  namespace: string;
  name: string;
  uid: OpaqueId;
  revision: OpaqueId;
};
type Page<T> = { items: T[]; next_after?: OpaqueCursor };
type RuntimeConfiguration = {
  harness: Harness;
  model: string;
  reasoning_efforts: string[];
};
type EnvironmentDescriptor = {
  target: TargetRef;
  execution_account: AccountRef;
  readiness: { state: "ready" } | { state: "not_ready"; reason: string };
  workspace: { root: string }; // absolute; supplied by runtime/configuration owner
  instructions: string;
  configurations: RuntimeConfiguration[];
  observed_at: Timestamp;
};
type TemplateDescriptor = {
  template: TemplateRef;
  backend: "pod" | "vm";
  configuration: {
    workload: SandboxTemplateSpec;
    execution_account: AccountRef;
    bootstrap?: string;
    wake_on_send: boolean;
    idle_suspend_after_seconds?: integer; // >= 60; absent means no idle suspension
  };
};
type CreationObservation = { creation_id: UUID; source: "environment" } & (
  | { kind: "not_observed" }
  | { kind: "provisioning" }
  | { kind: "created"; environment: EnvironmentDescriptor }
  | { kind: "failed" | "unknown"; error: ToolError }
);
```

`TemplateDescriptor` projects deployment-published `SandboxTemplate` configuration and launch
defaults, resolved with the verified caller's execution account. It is not a new persisted template
entity. Its revision covers the complete resolved configuration, including the execution account. `SandboxTemplateSpec` reuses the
[existing public Pod/VM and volume schema](../sandboxes/api/types.go); Kubernetes quantities retain
their explicit units. `Harness` reuses the [Runner enum](../proto/session.proto), limited to offered
non-unspecified values. Unsupported or unavailable runtime configuration is explicit; a failed
discovery read cannot become an empty successful list.

`not_observed` means only that the backend has no creation fact yet; while an Action is pending
approval this is expected. It is not proof that a timed-out creation had no effect. `provisioning`
means durable backend admission. `created` proves a Sandbox UID exists; readiness is a separate
observation. `failed` requires a known terminal failure; otherwise return `unknown` with retained
facts and the read-only recovery lookup. Backend errors return `ToolError`, not invented emptiness.

### Session observations and errors

```typescript
type SessionSpec = {
  harness: Harness; // an offered, non-unspecified value
  model: string; // nonempty offered ID
  cwd: string; // absolute; existing backend path validation still applies
  reasoning_effort?: string; // an offered value
};
type SourcePart<T, S> =
  | { kind: "observed"; source: S; observed_at: Timestamp; value: T }
  | { kind: "not_observed"; source: S }
  | { kind: "unavailable"; source: S; error: ToolError }
  | { kind: "partial"; source: S; observed_at: Timestamp; value: T; error: ToolError };
type SessionSnapshot = {
  session_id: UUID;
  opening: SourcePart<Dispatcher.SessionOpening, "dispatcher">;
  history: SourcePart<History.SessionSummary, "history">;
};
type HistoryCoverage = {
  source: "history";
  through_cursor?: string; // decimal uint64 in protobuf JSON, not a client page cursor
  gaps: History.CursorRange[];
  resume_token?: OpaqueToken;
  error?: ToolError; // may accompany retained facts when following/reading fails
};
type CorrelatedTurn = {
  turn_id: OpaqueId;
  input_command_ids: UUID[];
  status: TurnStatus; // completed-turn observation, not task success
  error?: string;
  final_assistant_message?: string;
};
type Observation = HistoryCoverage &
  (
    | { kind: "not_observed" }
    | { kind: "input_confirmed"; turn_id: OpaqueId }
    | { kind: "turn_completed"; turn: CorrelatedTurn }
    | { kind: "command_failed" | "command_no_op"; reason: string }
  );
type TurnView = {
  turn_id: OpaqueId;
  input: { text: string; command_ids: UUID[] }[];
  tool_calls: { item_id: OpaqueId; name: string }[];
  status?: TurnStatus; // absent until completion is observed
  error?: string;
  final_assistant_message?: string;
};
type TurnPage = HistoryCoverage & {
  session_id: UUID;
  turns: TurnView[];
  next_cursor?: OpaqueCursor;
};
type Recovery =
  | { tool: "get_action_request"; idempotency_key: string }
  | { tool: "environments__get_creation"; creation_id: UUID }
  | { tool: "environments__get"; target: TargetRef }
  | { tool: "environments__list" | "environments__list_templates"; limit: integer }
  | { tool: "sessions__get"; session_id: UUID }
  | { tool: "sessions__observe_input"; session_id: UUID; command_id: UUID; wait_seconds: 0 };
type ToolError = {
  code: string; // machine-readable service reason, not prose to parse
  source: "environment" | "dispatcher" | "history" | "runtime" | "mcp";
  effect: "not_started" | "unknown" | "not_applicable";
  details: {
    recovery?: Recovery;
    target?: TargetRef;
    creation_id?: UUID;
    session_id?: UUID;
    command_id?: UUID;
  };
};
type Failure<T> = { error: ToolError; observed?: Partial<T> };
```

The proposed tool's `SessionSpec` contains runtime settings only; `additional_instructions` is an
opening argument. Backend adaptation fills the existing protobuf instructions field according to
the composition contract above. This is new facade/composition work, not an existing protobuf
field named `additional_instructions`.

Every success schema in the table also permits `Failure<SuccessSchema>`. A backend/follower
failure preserves any facts already observed and an explicit error. Missing History, a retention
gap, and an unavailable source remain distinct. `SessionSummary.attached` supplies the existing
setup/harness state; `last_turn_status` does not establish task success or identify the current turn.

The observation helper follows History's command-to-turn association through bounded pages,
rather than taking the next completed turn as the answer. Several input IDs can contribute to one
turn. A missing association stays unobserved; proven gaps stay explicit. When observation fails,
`not_observed` with an error is not an empty successful read. Resume tokens are scoped to the
Session/command and confer no permission. Lookups after a lost reply enforce the same authority.

Dispatcher delivery states are returned unchanged, including failure, cancellation and unknown.
Queued cancellation takes the original input command UUID. Interruption takes a fresh command UUID
and the exact observed turn ID; its immediate response is not a new durable Dispatcher queue.
Setup is part of the opening contract, with completion observed separately through `sessions__get`.

The implementation must still prove that native and central History retain the necessary
correlations and payloads; absent text is not reconstructed from another turn. Agent usability
acceptance includes reading these descriptions/schemas and recovering without private handles.

## Failure, communication and lifecycle checks

| Situation                                | Agent-facing behavior and backend obligation                                                                                                                                                                                           |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Lost open/input reply                    | Recover by retained Session/command ID, or exact same request under Dispatcher's deduplication contract; never mint a new ID to conceal uncertainty. Authenticate lookup as strictly as mutation.                                      |
| Target deleted/recreated                 | Admission checks expected Sandbox UID at Dispatcher, atomically with choosing the stored target. Never redirect to the same name's replacement. Current `OpenSession` lacks an expected UID field; a facade pre-check is insufficient. |
| Follow-up races another input            | Correlate by command ID; show shared turn IDs and all contributing inputs. Do not invent one-task-per-turn semantics.                                                                                                                  |
| Observation expires / client disconnects | Return retained facts/cursor; work continues. A disconnected follower says nothing about the child process or task outcome.                                                                                                            |
| Coordinator or JS host restarts          | Reattach through durable Session/command IDs. Serialize references in retained client state; do not rely on live JS bindings or claim `store()` survives host loss.                                                                    |
| Cancel races delivery                    | Report actual queued cancellation or too-late/current dispatch state. Interrupt a known running turn separately; interruption is not rollback of external effects.                                                                     |
| Child says it is blocked                 | That is model-authored output. The first slice does not manufacture a backend blocked-task state. Select a typed report tool separately if this proves insufficient.                                                                   |
| Target removed after completion          | Read authorized retained evidence via the recorded Session association; a live Sandbox must not be required merely to read a retained result. Do not infer a replacement SA's authority from its name.                                 |

Existing peer correspondence can supplement input/result reads. `SendMessage` carries a body,
caller key and destination; it authenticates the sender SA, while `from_session` is an unverified
label. `covered` and `acknowledged` describe inbox delivery/acknowledgement, not task acceptance.
A child can send a JSON body to an explicitly supplied parent destination **if independently
authorized**; it is model-authored content, not a new report protocol. External inboxes/push are
not required for the MCP workflow. Inbox/history tags remain revisable implementations; neither
a reply address nor a creator/parent edge grants send/read access.

## Remaining work and gates

- **Natural-agent usability:** complete the capstone below using the implemented discovery,
  guidance, tools and result projections. The scripted external MCP and parent CLI suites are
  landed protocol/workflow evidence, not proof of unaided discovery. The operator will test with
  a prompt later; this is not a merge or rollout gate (operator, 2026-11-02 UTC).
- **Staging direct-tool exposure:** [#13514](https://github.com/agentydragon/ducktape/pull/13514)
  now configures the nine-tool contract, including `sessions__get` and `sessions__read_turns`.
  Validate the rendered catalogue and exact-head CI; preserve `basic` auto-approval and effective-caller enforcement.
  Activation and rollback are separate from the merged backend and guidance rollout.
- **Future provisioning:** borrowed-SA/no-new-grants identity and pinned approved configuration
  remain deferred until catalogue ownership is selected. Reuse persisted caller UID; creation
  remains outside the first release.

Recording a logical principal does not itself route execution credentials or provider billing.
Implementation must check the effective account for execution, egress and model access; for this
slice it must match the selected target's actual account. Caller SA UID, target SA UID, backend
peer, evidence writer and optional claimed parent remain independently represented.

The held cross-SA History decision remains held. The narrow read decision above needs only the
association and checks required for these same-principal workflows; implementing history tags,
all-Sandbox grants or a general delegation hierarchy is not a prerequisite. Existing
`orchestration_v1` broker grants remain explicit until
[`ORCHESTRATION_V1_BACKEND_GRANTS`](task_dag.md#orchestration_v1_backend_grants--orchestration_v1-operations-on-backend-grants)
has a reviewed migration. Avoid a second general authorization owner in the new facade.

## Remaining usability capstone and exposure order

1. **Enable the reviewed staging catalogue.** The operator approved merging #13514 after
   automated checks, with activation/rollback instructions retained. Backend and guidance rollout
   gates already discharged are not new work.
2. **Evaluate natural delegation through normal use.** Give a real agent an
   ordinary assignment and verify it discovers the target and schemas, prepares its directory,
   opens a child, submits separate input and reads a correlated result. Include follow-up and
   control/recovery when needed. Cover an existing-environment parent and an external MCP caller
   without private fixture handles or a prescribed tool script. The operator may run a prompt
   later; it does not gate staging activation. Replan concrete backend gaps if discovered.

Reuse the scripted suites and owning service tests for published-schema behavior, separate opening
and first input, command correlation and app-independent operation. Preserve authorization and
recovery requirements: foreign-SA denial, forged parent/principal, Session/command mismatch,
account/target replacement, read revocation, History lag/gaps, concurrent inputs and supported
retry/reconnect. Address concrete gaps in that coverage; the new usability exercise need not
repeat the entire matrix or the operator-waived sampler disruption drill.

An empty target list must clearly require an operator-prepared environment today. Exercise the
request/approval/creation workflow when future provisioning ships; do not invent a successful
launch before it exists. Owning the JS host, the full runtime model and native-child projection
remain unnecessary for the current MCP/CLI workflows.

Owning a JS host may later allow ordinary callable libraries, serializable environment handles,
cancellation and native errors such as `error.observed_output.text`. The pinned host's current
string-rejection adapter is not that API. This delegation slice works through MCP/CLI while the
separate host decision evaluates those broader ergonomics.

## Source map

- Current Action inputs/results and composition: [`contract/inputs.go`](../action_service/orchestration_v1/contract/inputs.go),
  [`sessions.go`](../action_service/orchestration_v1/sessions.go),
  [`reads.go`](../action_service/orchestration_v1/reads.go),
  [`turns.go`](../action_service/orchestration_v1/turns.go),
  [`presets.go`](../action_service/orchestration_v1/presets.go).
- Current broker workflow evidence: [`scenarios/mcp.py`](../system_tests/scenarios/mcp.py),
  `launcher_drives_only_the_sandbox_it_launched`; deployment policy:
  [`staging.py`](../../cluster/cdk8s/agentplane/staging.py).
- Command ownership/deduplication/cancellation: [`session_dispatcher.proto`](../runner_gateway/dispatcher/session_dispatcher.proto),
  [`api.go`](../runner_gateway/dispatcher/api.go); central caller:
  [`central_runner.go`](../runner_gateway/dispatcher/central_runner.go).
- Identity/authentication: [`central sampler service`](../central_sampler/service.go),
  [`History ingress`](../history_service/ingress/ingress.go),
  [`History storage`](../history_service/storage/store.go),
  [`History API admission`](../history_service/api/service.go),
  [`workload auth`](../workload_auth/auth/auth.go),
  [`MCP core client`](../mcp_edge/core_client.go),
  [`Sandbox identity controller`](../sandbox_controller/identity.go).
- Agent bootstrap/tools: [`resolved.go`](../env/configuration/resolved.go),
  [`agent CLI`](../docs/agent_cli.md), [`MCP schemas`](../mcp_edge/mcpserver/tools.json),
  [`direct tools`](../mcp_edge/mcpserver/direct.go), [`MCP results`](../mcp_edge/mcpserver/results.go),
  [`peer messages`](../notification_service/notifications.proto).
- Tool design references: [Anthropic tool design](https://www.anthropic.com/research/writing-tools-for-agents),
  [tool definitions](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools),
  [MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).
  These inform the proposed surface; they do not assert that the repo has upgraded its protocol.
