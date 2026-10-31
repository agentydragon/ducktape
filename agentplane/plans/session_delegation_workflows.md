# Caller-privilege Session delegation: proposal for review

Status: **proposed; awaiting stakeholder review.** This chooses a bounded first workflow for
[`THREAD_CREATE_POLICY`](task_dag.md#thread_create_policy--authorize-session-creation-separately).
The tools below are proposed additions, not currently callable APIs. The source audit describes
the implementation at `9d845b6d0acf9ed60c72274e48e973267d258101`; the existing system-test
scenario was read, not executed for this proposal.

## Recommendation and decisions

Start with independent child Sessions in an existing, authorized environment, at the caller's
ServiceAccount authority. Support workload callers and external MCP callers through the same
backend checks. Keep opening a Session and submitting its first input separate. Provide a small
observation tool that correlates a command with its turn and returns structured facts.

| Decision for review | Recommendation                                                                                                     | Consequence / alternative                                                                                                                                                                              |
| ------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| First target scope  | Reuse an existing environment with the same SA UID as the caller                                                   | New environments with that SA need a controller/provisioning contract; include that work now if fresh-environment delegation is required for the first milestone.                                      |
| Session authority   | Record the logical execution principal at Dispatcher admission, separately from backend peers and evidence writers | Deriving authority only from a live environment makes retained reads and future environment-free Sessions depend on that environment's lifetime.                                                       |
| Agent interface     | Backend-authorized Session tools through MCP and CLI                                                               | An Action wrapper is also possible, but needs verified caller attribution and the same backend enforcement. Existing operator-approved orchestration remains a distinct authority path.                |
| Child results       | Observe input delivery and the correlated turn's output; follow up with another input                              | A task-level `report`/`blocked` protocol is additional product work. Neither turn completion nor inbox acknowledgement proves task success.                                                            |
| Required reads      | A narrow same-principal read/control contract for this workflow                                                    | This requires explicit approval here; it does not select the held, broader cross-SA archive policy. Without result reads, select another authenticated result-delivery contract before implementation. |

These are proposed choices, including authority and transport, rather than decisions already
made by the stakeholder. After review, split the selected work into independently finishable DAG
nodes with real dependencies and re-estimate the two-week sprint. Reliability remains P0; a new
authorization contract is substantial work and should not be assumed to fit the remaining sprint.

## What an environment means in this version

An execution target is an existing managed `Sandbox`, Pod or VM, identified by Kubernetes
`namespace`, `name` and immutable object `uid`. The tool calls it `target`; it does not introduce a
new environment object or Session-attachment entity. That UID distinguishes a deleted/recreated
Sandbox. Pod UID, VM instance, container restart and executor generation remain separate facts.

The caller may have no environment and no Agentplane Session: an external MCP connection is
sufficient authentication when its bound account is authorized. The child still needs one target
in this version. Native harnesses and the current central sampler require it; opening a central
Session currently acquires an executor and pins one Sandbox UID. Zero-environment children and
0..N attachments remain the [runtime north star](session_runtime_model.md), not a claimed feature
of this slice.

Sharing a target and giving each child a working directory matches the stakeholder's current
workflow. It creates no security isolation between those Sessions. Parentage is optional provenance
and confers no permission. No parent transcript is copied automatically; the creator supplies the
child's standing instructions and initial task.

## Proposed workflow A: agent reuses its environment

The platform instructions advertise the Session tools and how to discover targets. Today they
already supply the agent's notification address, including Sandbox identity, and put the same
JSON in `AGENTPLANE_DESTINATION`. Those identifiers are addresses, not credentials. Proposed target
discovery verifies whether that address is eligible rather than treating an environment variable
as authority.

The example is a CI-maintenance coordinator delegating investigation of one latency regression.
All names beginning `sessions_` below are **new MCP tools**. The equivalent proposed CLI group is
`agentplane sessions`; today's central code mode can reach a CLI through `exec`, but has no
`sessions` JavaScript global. MCP tools can be used by a native harness without Agentplane owning
its JavaScript host.

1. Discover tool schemas through MCP `tools/list`, or the proposed
   `agentplane sessions --help`. Read `sessions_list_targets` and call:

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
     "readiness": { "state": "ready" },
     "workspace": { "root": "/workspace" },
     "instructions": "Shared CI workspace; give each Session its own directory.",
     "configurations": [
       {
         "harness": "HARNESS_CODEX",
         "model": "offered-model",
         "reasoning_efforts": ["low", "high"],
         "cwd_policy": "workspace_descendants"
       }
     ]
   }
   ```

   `offered-model` denotes the actual model ID returned by discovery, not a literal to send or a
   model guessed from these docs. This whole descriptor is proposed. Configuration must come from
   the owning runtime or a shared deployment catalog checked against that runtime. Launch presets
   alone cannot establish what an existing target supports. Discovery includes freshness and
   pagination; readiness/configuration may change before admission. Cwd policy is backend-specific:
   native Runner enforces its configured workspace root; the current central sampler accepts an
   absolute path without that confinement. Discovery must describe the actual rule, not imply
   that placement in one Sandbox makes those checks equivalent.

2. Match the full target reference against the agent's supplied destination and choose an offered
   harness/model/effort. Inspect the existing repository with ordinary execution tools. Prepare a
   distinct directory or Git worktree under the returned workspace root, for example
   `/workspace/ci-latency-worker`. Directory preparation is an explicit command, not a hidden copy
   of the parent's repository. Runtimes already create missing cwd directories; repository setup
   needs explicit commands. A client without execution access can provide `setup_script` to
   `sessions_open`; setup completion is observable separately from opening.

3. Mint and retain a child Session UUID **before** sending. Call `sessions_open`:

   ```json
   {
     "target": { "namespace": "agentplane", "name": "ci-worker", "uid": "51b74ff8-9a36-4b55-a9f5-54806077782a" },
     "session_id": "d71434fa-f5ba-4fb4-b152-e31bd4b37c59",
     "spec": {
       "harness": "HARNESS_CODEX",
       "model": "offered-model",
       "reasoning_effort": "high",
       "cwd": "/workspace/ci-latency-worker",
       "instructions": "Investigate delegated CI latency work. Use this directory; state uncertainty and link evidence."
     }
   }
   ```

   Substitute the model discovered in step 1. The result contains the existing Dispatcher opening
   record, initially `DELIVERY_STATE_QUEUED_IN_DISPATCHER`. It proves durable admission of an opening,
   not that a model has accepted work. The target may be starting or suspended. An authorized wake
   uses the current wake policy; no promise that every suspended environment wakes automatically.

4. Mint and retain a command UUID; call `sessions_submit_input` separately:

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

5. Continue independent work, then call `sessions_observe_input`:

   ```json
   {
     "session_id": "d71434fa-f5ba-4fb4-b152-e31bd4b37c59",
     "command_id": "173c7199-ab88-4237-8553-831806e62c3c",
     "wait_seconds": 30
   }
   ```

   A possible proposed result:

   ```json
   {
     "session_id": "d71434fa-f5ba-4fb4-b152-e31bd4b37c59",
     "command_id": "173c7199-ab88-4237-8553-831806e62c3c",
     "dispatch": { "state": "DELIVERY_STATE_RUNNER_ADMITTED", "source": "dispatcher" },
     "observation": {
       "kind": "turn_completed",
       "source": "history",
       "turn": {
         "turn_id": "observed-harness-turn-id",
         "input_command_ids": ["173c7199-ab88-4237-8553-831806e62c3c"],
         "status": "TURN_STATUS_COMPLETED",
         "final_assistant_message": "The queue delay increased after ..."
       }
     },
     "resume_token": "opaque-observation-cursor"
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
execution host, parent Session or notification inbox.

1. `tools/list` exposes the new Session tools. `sessions_list_targets({"limit":20})` evaluates the
   connection's verified bound SA, rather than asking the model to provide one. With an eligible
   target, use the full reference and offered configuration exactly as in workflow A.
2. Call `sessions_open` with a retained UUID and usable absolute cwd, respecting the selected
   runtime's workspace rule. The runtime creates a missing directory. Repository preparation can
   use an explicit `setup_script`, with its result visible through `sessions_get`; a clone needs
   an explicit source and authorized credentials. Opening does not implicitly copy another repo.
3. Call `sessions_submit_input`, retain the command UUID, and use `sessions_observe_input` to read
   the correlated result. The external caller can reconnect and resume reads with those IDs.
   Returning the result does not require delivering a message into the external harness.

An account can legitimately receive an empty target list. Today's staging MCP account is
explicitly mintable as `claude-ai`; Sandboxes have their own distinct SAs. **Do not claim that
account automatically qualifies for same-SA reuse.** The first external reuse test must configure
an OAuth connection whose bound account is the target's actual SA, with explicit token-minting and
backend-audience permission. That is deployable configuration work, not a caller-supplied override
or permission to mint arbitrary workload tokens.

### When no eligible environment exists: current approved provisioning path

Today an external caller can request a fresh Pod/VM Sandbox through `orchestration_v1`. This is a
separate operator-approved broker path: the launched Sandbox gets a **new SA**, not the caller's.
Keep it usable while choosing the same-SA provisioning contract; do not relabel it inheritance.

The real current sequence is:

| Step               | Existing operation and required data                                                                                                                                                                                                                         |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Discover           | `list_actions({"group":"orchestration_v1"})`, then `get_action` with `include_fields:["description","input_schema"]`; inspect `get_action_policy` rather than inferring access from the catalog.                                                             |
| Choose launch data | Request `orchestration_v1/list_launch_presets`; copy a returned preset's concrete `launch` and `open_session` fields. A preset name is not an argument or authority.                                                                                         |
| Provision          | Request `launch_sandbox` with `name`, returned template/bootstrap/policy fields, and explicit `launcher_grants` such as `open_session`, `submit_input`, `get_session`, `read_turns`, `wait_for_turn`. Current staging requires operator approval for launch. |
| Obtain handle      | The launch result returns `sandbox:{name,uid}`. The Action binding fixes the namespace; adding `namespace` inside this current Action's `sandbox` argument is invalid.                                                                                       |
| Open               | Request `open_session` with that `sandbox`, a caller-minted `session_id`, and the returned Session spec/setup fields. It queues before Ready.                                                                                                                |
| Establish cursor   | After the Session is visible to History, call `read_turns({sandbox,session_id,count:1})` before input and retain `resume_token`. `get_session` distinguishes an opening from a History record.                                                               |
| Submit             | Request `submit_input` with the same reference/Session, a fresh `command_id`, and `text`.                                                                                                                                                                    |
| Wait and correlate | Call `wait_for_turn` with the cursor and bounded `wait_seconds`; read turns and match `input[].command_ids`. The first completion after a cursor might belong to another command.                                                                            |

For each Action above, the existing MCP envelope is:

```json
{
  "request": {
    "idempotency_key": "caller-retained-action-key",
    "action": { "group": "orchestration_v1", "name": "open_session" },
    "arguments": {
      "sandbox": { "name": "returned-name", "uid": "returned-uid" },
      "session_id": "caller-minted-uuid",
      "spec": { "harness": "HARNESS_CODEX", "model": "returned-model", "cwd": "/workspace/ci-latency-worker" }
    },
    "title": "Open delegated CI investigator"
  },
  "wait": { "wait_seconds": 30, "wait_until": "terminal" },
  "respond_with": "result"
}
```

This is a schema illustration: replace returned values and obtain the spec from discovery. On
pending approval, inspect/follow that request; do not issue another launch. After a lost response,
use `get_action_request` or `get_action_result` by the **original Action key**. Repeated Action
submission keys are refused; they are not equivalent to Dispatcher's exact-request deduplication.

Current `<group>__<action>` direct tools generate the Action key on the server, so a lost first
reply can leave the caller without a recovery key. Use the generic envelope for this example's
mutations. The new first-party Session tools must keep caller-held IDs and recovery lookups. They
must not quietly inherit that direct-tool limitation. Current direct-tool visibility can change
with policy and has no `tools/list_changed` notification; cached listings confer no authority.

The existing `launcher_drives_only_the_sandbox_it_launched` system scenario covers this brokered
flow, including result correlation, a foreign Session, UID mismatch and grant revocation. It does
not prove automatic same-SA spawning or MCP-only account-token admission to Dispatcher/History.

## Tool contract derived from the workflows

The equivalent Go/protobuf schemas should reject unknown fields. All proposed MCP results use
`StructuredContent` plus a matching JSON text fallback. Callers never need to parse prose to learn
state, authority failure, IDs or recovery steps. Names below deliberately distinguish these tools
from the existing `orchestration_v1__*` broker facade.

| New tool                  | Input                                                                                                                          | Structured result / meaning                                                                                                                                                        |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sessions_list_targets`   | `limit:1..100`, optional opaque `after`                                                                                        | Page of full target refs, readiness, workspace/instructions, supported configurations and freshness; optional `next_after`. Empty is valid.                                        |
| `sessions_open`           | Full `target`, UUID `session_id`, explicit `spec:{harness,model,cwd,reasoning_effort?,instructions?}`, optional `setup_script` | Dispatcher `opening`. Same UUID/spec/target recovers the original; conflicting reuse fails. Cwd must be absolute and satisfy the selected runtime's advertised workspace rule.     |
| `sessions_submit_input`   | Full `target`, UUID `session_id`, UUID `command_id`, nonempty `text`                                                           | Dispatcher `dispatch`. Same exact command recovers its original admission; it never starts a new turn merely to recover a reply.                                                   |
| `sessions_observe_input`  | UUID `session_id`, UUID `command_id`, `wait_seconds:0..30`, optional `resume_token`                                            | Command receipt plus the discriminated History observation below. Bounded waiting is read-only. Both IDs must designate the same command/Session.                                  |
| `sessions_get`            | UUID `session_id`                                                                                                              | Opening, setup/harness state and current turn when observed, with source/freshness per part. Missing History is distinguished from missing Session; backend failures are explicit. |
| `sessions_read_turns`     | UUID `session_id`, `count:1..100`, optional page cursor                                                                        | Authorized retained turns and cursor, with explicit gaps. Debug/evidence reads remain separate from this compact view.                                                             |
| `sessions_cancel_input`   | UUID `session_id`, UUID `command_id`                                                                                           | Existing Dispatcher's cancellation result; only a queued, unclaimed dispatch can be cancelled. A running turn requires interruption.                                               |
| `sessions_interrupt_turn` | Full `target`, UUID `session_id`, UUID `command_id`, observed `turn_id`                                                        | Existing immediate interrupt result, including no-op/refusal. It targets that turn, not a later one, and requires a reachable runtime.                                             |

The tool descriptions must explain those distinctions, the relevant discovery operation and the
recovery ID to retain. No arbitrary name lookup, silent model selection, authority override,
implicit environment creation or destructive Session deletion is part of these operations.

The proposed observation union is equivalent to:

```typescript
type Observation =
  | { kind: "not_observed"; source: "history"; resume_token?: string }
  | { kind: "input_confirmed"; source: "history"; turn_id: string }
  | { kind: "turn_completed"; source: "history"; turn: CorrelatedTurn }
  | { kind: "command_failed" | "command_no_op"; source: "history"; reason: string };

type CorrelatedTurn = {
  turn_id: string;
  input_command_ids: string[];
  status: string; // Existing TurnStatus enum; not a task-status enum.
  error?: string;
  final_assistant_message?: string;
};
```

Dispatcher delivery states are returned separately and unchanged, including failure, cancellation
and unknown. Several command IDs can contribute to one turn. The observation helper follows that
association through History, rather than taking any next completed turn as the answer. It scans
from a durable admission cursor where available and uses bounded history pages with continuation
otherwise. An absent/gapped association must remain unobserved or an explicit gap, never be guessed
from temporal proximity. Tokens are bound to the Session/command and confer no permission.

Recoverable failures return `error:{code,source,details}`, relevant retained IDs, and an explicit
`effect` of `not_started`, `unknown`, or `not_applicable`. `details.recovery` names the lookup or
observation to use; it does not instruct the agent to replay uncertain work. Human explanation may
accompany these fields but is not the only record. This is new work: some current MCP failures
carry details only in text. A partial observation may carry both facts collected so far and a
History/Dispatcher error; an unavailable source must not be presented as an empty successful read.

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

## Backend gaps and implementation ownership

| Gap confirmed by source audit                                                | Owning change / constraint                                                                                                                                                                                                                                                                                                                                                                |
| ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dispatcher admits a fixed backend allowlist rather than same-SA target scope | Dispatcher authenticates normal callers, checks target's actual SA UID, records logical principal and enforces every write, lookup, list, watch and cancel. Keep explicit privileged broker operations distinct.                                                                                                                                                                          |
| Current identity records can identify intermediary services                  | `sampler_session.account_uid` currently identifies its authenticated Dispatcher peer; central History's writer is the sampler SA. Neither can be reinterpreted as the child/creator's authority. Add explicit logical-principal/provenance fields and validate their source.                                                                                                              |
| No trusted retained logical-principal association for the proposed read rule | Dispatcher owns admission and supplies its immutable association over an authenticated, designated backend path to sampler/History. History stores it separately from writer and execution Sandbox. Ordinary callers cannot assert another principal. Preserve existing evidence; unassociated old records need an explicit adoption policy, not guessed ownership.                       |
| Account-token external callers do not fit every current workload method      | MCP edge resolves the verified OAuth connection and its allowed account UID. Reuse `ResolveServiceAccountToken` for explicitly account-authorized methods/audiences; current Dispatcher/History workload admission requires Pod binding. Limit TokenRequest permissions and recheck account identity/revocation. Do not relax every workload endpoint or trust a forwarded caller string. |
| Existing presets describe fresh launch inputs, not eligible existing targets | Dispatcher target inventory plus runtime/configuration owners expose a bounded, authorized descriptor. Extract any shared configuration from the app; no backend calls the app or reads its private tables. Runtime admission remains authoritative.                                                                                                                                      |
| `OpenSession` pre-check cannot fence replacement during the RPC              | Add expected UID to the backend opening contract and check it there, including deduplication. Scope all subsequent lookups to authenticated authority.                                                                                                                                                                                                                                    |
| Raw wait/read flow is cumbersome and not command-specific                    | Neutral shared client projection implements `observe_input` over scoped Dispatcher and History APIs. CLI/MCP invoke both as the caller, without a new privileged result database, queue or task owner. Reuse existing command correlation/folding where possible.                                                                                                                         |
| Direct MCP mutation recovery and structured failure details are incomplete   | New tools preserve caller-held IDs, offer lookup, return typed structured failures and use the same authorization as raw clients. CLI exposes the equivalent contract with machine-readable output.                                                                                                                                                                                       |
| Current launch creates a separate Sandbox SA                                 | Controller/provisioner needs a reviewed selected/shared-SA ownership and lifecycle contract before offering same-SA new environments. Action execution boxes already have a related caller-SA path; unify provisioning semantics where applicable rather than equating unlike current APIs.                                                                                               |

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

## Implementation sequence after selection

1. **Pin admission/identity and read contracts.** Specify trusted association propagation,
   target UID fencing, exact same-SA rule, revocation and existing-record handling. A shared
   execution account is a real authority boundary; per-Session credentials are not introduced.
2. **Implement backend scope and target/configuration discovery.** Mutation and result-read
   authorization are prerequisites for exposing a working delegation loop. MCP account-token
   admission and workload admission can be implemented independently against that selected contract.
3. **Expose MCP/CLI tools and observation projection.** Schemas/client work can proceed against the
   pinned contract; end-to-end usability waits on the enforcing backends. No dependency on owning
   the JS host, changing the whole environment model, or projecting native harness subagents.
4. **Exercise both workflows; revise scope if they fail.** Add same-SA provisioning only after its
   contract is selected. This is a sequencing recommendation, not a technical dependency of target
   reuse or a promise to finish all phases inside the current sprint.

Exit evidence must include complete executable examples using only published schemas/instructions:
target discovery, working directory preparation, independent child opening, first input,
command-correlated result, follow-up, queued cancellation/running-turn interruption, and lost-reply
recovery. Cover native and central harnesses where offered, an external MCP caller with no local
environment, and an empty target list with the explicit provisioning route. Run with the app
unavailable. Test foreign-SA denial, forged parent/principal, Session/command mismatch, SA and
Sandbox replacement, read revocation, History lag/gaps, concurrent inputs and service rolls. A
scripted harness proves protocol behavior; an agent exercise must separately show it can discover
and use the interface without private fixture handles or undocumented help.

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
- Identity/authentication: [`central sampler service`](../x/central_sampler/service.go),
  [`History ingress`](../history_service/ingress/ingress.go),
  [`History storage`](../history_service/storage/store.go),
  [`History API admission`](../history_service/api/service.go),
  [`workload auth`](../workload_auth/go/auth/auth.go),
  [`MCP core client`](../mcp_edge/core_client.go),
  [`Sandbox identity controller`](../sandbox_controller/identity.go).
- Agent bootstrap/tools: [`resolved.go`](../runner/configuration/resolved.go),
  [`agent CLI`](../docs/agent_cli.md), [`MCP schemas`](../mcp_edge/mcpserver/tools.json),
  [`direct tools`](../mcp_edge/mcpserver/direct.go), [`MCP results`](../mcp_edge/mcpserver/results.go),
  [`peer messages`](../notification_service/notifications.proto).
- Tool design references: [Anthropic tool design](https://www.anthropic.com/research/writing-tools-for-agents),
  [tool definitions](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools),
  [MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).
  These inform the proposed surface; they do not assert that the repo has upgraded its protocol.
