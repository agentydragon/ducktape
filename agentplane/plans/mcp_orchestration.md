# Orchestrating Agentplane agents from an MCP client

Status: **proposed 2026-10-26; nothing implemented, no phase authorized.** The operator (23:19 UTC) asked how
Claude, connected through Agentplane's MCP server, could "orchestrate agentplane agents to work on tasks": which
capabilities it needs, how they reach an agent talking to the MCP server "(possibly through an actions sandbox it
might allocate)", the planned workflow, and how this fits the RBAC and identity philosophy. Today only messages
between Session inboxes exist, and reading an inbox, making a Sandbox or opening a Session from the MCP side would
be "pretty annoying". Follow-up (23:21): "i might be fine for now with some of these actions needing actions
approval rather than executing by some mechanism that does not need human approval, if it makes things easier."

Claude.ai is the first client, but nothing below is specific to it: the same surface serves any MCP Connection, and
an orchestrator in a Session Sandbox reaches the same Actions through the Action Service API and the
[`agentplane` CLI](../docs/agent_cli.md) ([user stories 2 and 3](user_stories.md#2-trusted-orchestrator-untrusted-fleet)).

## The workflow

What an orchestrating caller does, with the working tool names of the [tool surface](#tool-surface):

1. **Pick a configuration.** `orchestration__list_launch_presets` returns the reviewed launch presets resolved to
   concrete fields: template, policy sets, inbox tags, bootstrap and Session defaults.
2. **Launch a worker.** `request_action` for `orchestration/launch_sandbox` with those fields, edited for the task,
   plus the grants the caller wants on the new Sandbox. The operator approves it on the Action card, which Web Push
   brings to the phone. The result names the Sandbox and its UID.
3. **Give it the task.** `orchestration__open_session` with the Sandbox name and UID, a Session ID the caller mints
   and the Session spec, then `orchestration__submit_input` with the task. The Dispatcher queues both until the
   Sandbox is Ready, so neither waits for step 2's Pod to start.
4. **Follow it.** `orchestration__get_session` says whether the Session is working or idle and where its queued
   commands are. `orchestration__wait_for_turn` waits up to the instance's direct-tool wait (30 s on staging) for
   the running turn to end. `orchestration__read_turns` returns completed turns' final assistant messages and
   outcomes, paging further back on request. These two wait on History's
   [read and follow API](#reading-turns-depends-on-historys-read-and-follow-design), which is not designed yet.
5. **Answer and steer.** A worker that needs something ends its turn saying so. The caller reads that in step 4 and
   answers with `orchestration__submit_input`, or stops a turn going wrong with `orchestration__interrupt_turn`.
6. **Finish.** Launched with idle suspend and wake-on-send, the worker suspends once idle and a later input wakes
   it. Its work lands where its own grants let it write, such as a branch or a PR. Deleting it stays an operator
   act, never automatic.

Steps 3 to 5 repeat per worker and across workers. The operator sees every launched Sandbox and its Threads in the
app and can step into any of them. The Sandbox records which caller launched it through which Action request; that
is provenance and grants nothing.

A claude.ai conversation acts only while it has a turn, so following is pull-based; a Claude Code session can also
schedule its own next check. Neither needs an inbox of its own in v1, since a worker's reply is its turn outcome,
read in step 4. [External inboxes](task_freezer.md#external_agent_inboxes--inboxes-for-externally-running-agents)
stay deferred until a worker must reach a caller that has not asked.

`submit_input` and messages are different channels. An input is a user turn the Dispatcher delivers, what the
operator types in the app; it fits the Sessions a caller opened. A message is a Notifications inbox entry with a
notice, which the agent reads and acknowledges; it fits Sessions the caller may write to but did not open.

## What exists, and what stops `claude-ai` today

The claude.ai Connection acts as the `claude-ai` ServiceAccount ([MCP edge authority](../mcp_edge/README.md#authority)).

| Capability                                            | Owner                                                                         | Today                                                            | For `claude-ai`                                                                                                                                                                                                                                                                               |
| ----------------------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A shell as itself                                     | Actions, `sandbox__*`                                                         | Shipped                                                          | Works; its boxes hold `basic`, which reaches Notifications and Actions                                                                                                                                                                                                                        |
| Read Sandboxes                                        | Kubernetes                                                                    | Shipped                                                          | Works through `kubectl` in a box                                                                                                                                                                                                                                                              |
| Launch a Session Sandbox                              | the app, as the operator, through the shared `agentplane/sandboxes` inventory | Operator only                                                    | No Sandbox writes; [`AGENT_LAUNCH_POLICY_DESIGN`](task_dag.md#agent_launch_policy_design--sandbox-launch-and-delegation-policy) is open                                                                                                                                                       |
| Open a Session, submit input, interrupt, change model | Dispatcher                                                                    | Only the app and Notifications, trusted to authorize their users | Not an admitted caller; [`THREAD_CREATE_POLICY`](task_dag.md#thread_create_policy--authorize-session-creation-separately) and [`DISPATCHER_SEND_AUTHORIZATION_GENERALIZE`](task_dag.md#dispatcher_send_authorization_generalize--replace-v0-service-trust-with-scoped-sender-grants) are open |
| Message a Session's inbox                             | Notifications, `session_inbox_access`                                         | Shipped                                                          | From a box, once a binding gives it a `send-to-*` set; it holds none                                                                                                                                                                                                                          |
| Read its own inbox                                    | Notifications                                                                 | Shipped for Sandbox accounts                                     | Has none: no Sandbox runs as it                                                                                                                                                                                                                                                               |
| List Sessions, read or follow history                 | History                                                                       | Configured `reader_accounts` only (the app, Notifications)       | Refused; [`THREAD_READ_POLICY_DESIGN`](task_dag.md#thread_read_policy_design--scoped-history-read-policy) is held, [`HISTORY_READ_FOLLOW_DESIGN`](task_dag.md#history_read_follow_design--read-and-follow-one-sessions-history) is open                                                       |
| Suspend, resume, delete                               | the app, as the operator                                                      | Operator only                                                    | [Deferred](task_freezer.md#sandbox_lifecycle_authorization_design--delegated-lifecycle-controls)                                                                                                                                                                                              |

So messaging works only from a box and only into Sessions someone else opened; nothing else has a caller path.

## Identity and authority

The rules this plan keeps, from the [principles](../docs/principles.md), [policy sets](../docs/adr_policy_sets.md)
and the [cross-agent plan](multiagent_communication.md):

- **A caller is a ServiceAccount.** An MCP Connection acts as the account it is bound to, an in-cluster
  orchestrator as its Sandbox's account. Tool arguments never name the caller. Attribution is per account, so every
  claude.ai conversation of the operator is one caller, `claude-ai`.
- **Authority is a typed rule in a live `PolicySet`,** assigned by a `PolicyBinding` and enforced by the service
  that owns the resource. There is no orchestrator type: orchestrating is a set of grants any account can hold, and
  a preset carries none.
- **The Sandbox is the grant and isolation boundary.** Rules target Sandboxes directly (by name, optionally
  UID-pinned), by tag or with `allSandboxes`; a Session ID only addresses.
- **Nothing is inherited.** A worker runs as its own Sandbox's account with the sets its launch names. It never
  gets the launcher's grants, which for `claude-ai` include Haku's mailbox and Forgejo credentials and the Google
  reads, and the launcher gets nothing on the worker beyond what it is explicitly granted. Creator is provenance.
- **Content that crosses is information transfer.** A task the caller writes reaches the worker and the worker's
  model provider. A worker's reply is untrusted input to the caller, never an instruction or a grant.

An operation can be authorized in two ways:

- **By the caller's grant.** The owning backend checks a rule bound to the caller, as Notifications checks
  `session_inbox_access` on a send. An Action approval in front of it is an extra human gate and adds no authority,
  as in the Sandbox Actions' [shell as yourself](../action_service/sandbox/README.md#the-contract) contract.
- **As the operator's act.** For an operation no backend can grant a caller yet, the request is an Action whose
  Decision is the authority: the operator's approval, or an `action_auto_approve` rule the operator wrote, which is
  a standing delegation. An executor then performs it under a service identity that the backend trusts as the
  Dispatcher trusts the app to act for its operator. This is what the operator offered at 23:21, and what the
  `kubernetes_admin` group already does with the operator's own Kubernetes authority.

V1 uses the operator's act for launching, Session commands and history reads, and moves each operation to the
caller's grant when its backend rule type lands ([phases](#phases)).

### Grants on a Sandbox the caller launched

A hand-written rule cannot name a Sandbox that does not exist yet. A launch request therefore carries
`launcher_grants`, the operations the caller wants on the new Sandbox, and the executor writes them as a `PolicySet`
and `PolicyBinding` owned by that Sandbox, UID-pinned to it, bound to the caller and deleted with the Sandbox. This
widens the operator's suggestion for messaging under `AGENT_LAUNCH_POLICY_DESIGN` (2026-10-21) to every operation
here:

- `send_message` as a `session_inbox_access` rule, which Notifications enforces now;
- the `orchestration` Actions on that Sandbox as `action_auto_approve` rules matching its name and UID, each
  replaced by its backend rule type once one exists.

The launch card shows these grants, so approving a launch approves them, and a request without them gets none. One
tap per worker then covers all of that worker's Session commands and reads. The Sandbox's Policies tab lists the
binding like any other.

```yaml
# Written by the executor when the operator approves claude-ai's launch of worker-3f2a.
apiVersion: agentplane.allegedly.works/v1alpha1
kind: PolicySet
metadata:
  name: worker-3f2a-launcher
  namespace: agentplane-staging
  ownerReferences: [{ apiVersion: agents.x-k8s.io/v1beta1, kind: Sandbox, name: worker-3f2a, uid: 9b0e… }]
spec:
  rules:
    - type: session_inbox_access
      operations: [send_message]
      targets: { sandboxes: [{ name: worker-3f2a, uid: 9b0e… }] }
    - type: action_auto_approve
      policy:
        type: argument_schema
        actions:
          orchestration:
            [open_session, submit_input, interrupt_turn, get_session, list_sessions, read_turns, wait_for_turn]
        schema:
          required: [sandbox]
          properties: { sandbox: { const: { name: worker-3f2a, uid: 9b0e… } } }
---
apiVersion: agentplane.allegedly.works/v1alpha1
kind: PolicyBinding
metadata:
  name: worker-3f2a-launcher
  namespace: agentplane-staging
  ownerReferences: [same owner]
spec:
  subject: { namespace: agentplane-staging, name: claude-ai }
  policySets: [worker-3f2a-launcher]
```

The executor also checks at execution that the named Sandbox still has the given UID, so a same-name replacement
never inherits a grant meant for its predecessor.

## Exposure

**One Action group, `orchestration`, holds every operation**, as an executor kind in the Action Service beside the
Sandbox group's. A claude.ai conversation reaches it through the MCP edge like any Action: the auto-approvable calls
appear as [direct tools](../action_service/README.md#direct-tools) and answer in one call, while the rest go through
`request_action` and wait for a Decision. An in-cluster orchestrator submits the same Actions with
`agentplane actions submit orchestration/<name>`. No new MCP server, edge tool or app endpoint is involved.

The executor lives in the Action Service because it needs the authenticated `ExecutionRequest.caller`, to bind
launcher grants and to record provenance, and the dispatch lease and receipts every Action has. Its service account
gains `create` on Sandboxes, PolicySets and PolicyBindings in the Sandbox namespace, an entry among the Dispatcher's
callers and wake callers, and one among History's readers. The Action Service already creates Pods as any caller
account in that namespace and holds `pods/exec` there, which reaches Session Sandboxes too
([ADR](../docs/adr_action_sandboxes.md)); the new rights add Session commands and history reads, each behind a
Decision.

**The tool surface stays the same through the migration.** When a backend gains a rule type for an operation, the
executor switches that operation from its service identity to a token for the caller's own account, minted at
execution and accepted without a Pod binding as Actions accepts the edge's, and the launcher grant for it becomes the
backend rule. Tool names and arguments stay; only who is checked changes.
Messaging joins the group the same way, as a caller-mode `send_message`.

**The Action box stays useful, not required.** It remains the place to script many calls (`agentplane actions` in a
loop, `jq` over the results) and to reach whatever the caller's egress allows. It cannot do anything the caller's
account cannot, so on its own it does not close any gap in the table above.

### Tool surface

Working names; the group key and Action names are open.

| Action                                                                      | What it does                                                                                                                  | Calls                                                                                                     | Default decision           |
| --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `list_launch_presets`                                                       | The reviewed presets, resolved to concrete launch and Session fields                                                          | the executor's copy of the preset catalog                                                                 | Auto, by a Flux-owned rule |
| `launch_sandbox`                                                            | Create a Session Sandbox from concrete fields, idle suspend and wake-on-send included; write `launcher_grants`                | Kubernetes, through the Sandbox request builder the app uses                                              | The operator, every time   |
| `open_session`                                                              | Open a Session with its spec and setup script, under a Session ID the caller mints                                            | Dispatcher `OpenSession`                                                                                  | Launcher grant             |
| `submit_input`, `interrupt_turn`, `change_model`, `change_reasoning_effort` | One Dispatcher command each, keyed by a caller-chosen `command_id`                                                            | Dispatcher                                                                                                | Launcher grant             |
| `get_session`                                                               | The Sandbox's `Ready` condition, the Session's listing fields and its Dispatches                                              | Kubernetes, History `FindSessions`, Dispatcher `ListDispatches`                                           | Launcher grant             |
| `list_sessions`                                                             | A Sandbox's Sessions with the [agreed listing fields](multiagent_communication.md#sandbox-session-discovery-and-provisioning) | History                                                                                                   | Launcher grant             |
| `read_turns`                                                                | Completed turns' final assistant messages and outcomes, newest first, paged back                                              | History's read and follow API, [once designed](#reading-turns-depends-on-historys-read-and-follow-design) | Launcher grant             |
| `wait_for_turn`                                                             | Wait, up to the instance's wait limit, for the running turn to end                                                            | The same API                                                                                              | Launcher grant             |

The same tools reach a Sandbox the caller did not launch, such as the operator's own Haku: each call then waits for
the operator, unless a Flux-owned rule grants that Sandbox's tag.

Retries are safe because the caller supplies the keys: the Dispatcher deduplicates `OpenSession` on `session_id`
and every command on `command_id`, so a direct-tool call repeated after a lost reply does not submit twice, which
the [direct-tool retry limitation](../action_service/README.md#direct-tools) would otherwise allow. `launch_sandbox`
always goes through `request_action` with a caller-chosen idempotency key.

Launch takes concrete fields, never a preset name, so what the operator approves is what runs and no backend
attaches meaning to a preset name ([launch presets](../docs/launch_presets.md)). The catalog is app configuration
today; cdk8s renders it into the executor's settings from the same definition, so the executor reads no app API or
app state ([dependency rule](../docs/service_boundaries.md)).

Not in v1: suspend, resume and delete. Idle suspend and wake-on-send cover a worker's cost, and deleting stays an
operator act in the app.

### Reading turns depends on History's read and follow design

How History's gRPC reads and watches should work is open
([`HISTORY_READ_FOLLOW_DESIGN`](task_dag.md#history_read_follow_design--read-and-follow-one-sessions-history), operator
2026-10-26: "there is still an open issue about how exactly reads/watches from history service via grpc should
work"). This plan takes that design as a prerequisite, not as settled: `read_turns` and `wait_for_turn` are built on
whatever API it chooses, and are not built on today's paged reads and payload-free summary watch in the meantime,
which would add one more follow loop for that design to move ([#11817](https://github.com/agentydragon/ducktape/issues/11817)
is what such a loop got wrong for titles). What an orchestrator needs from it: a start a number of turns back, a kind
filter for turn completions and final assistant messages, and a bounded wait for the next turn end that a caller with a
30 s tool budget can repeat without a gap or duplicate. [History read and follow](history_read_follow.md) proposes one
call for that design and
[how these two tools build on it](history_read_follow.md#what-the-orchestration-executor-builds-on-it).

Until then a caller sees progress through `get_session` (idle or working, Dispatch states), and a worker reports
results where its own grants let it write.

## Where this departs from current rules

Each departure is transitional, is removed when the operation's backend rule lands, and gets a removal node in the
[task DAG](task_dag.md). The operator accepted the first two on 2026-10-26 (23:37 UTC).

- **The Dispatcher gains a third trusted caller.** `DISPATCHER_SEND_AUTHORIZATION_GENERALIZE` says not to extend
  the app and Notifications exception "to agents or other services". The executor acts only on a Decision, the
  operator's or an operator-written rule's, which is the same act the app performs for the operator. It also joins
  the wake callers, so an input wakes a suspended worker.
- **History gains a reader.** The [scoped-read plan](scoped_session_reads.md) says not to broaden the reader list
  wholesale, since a listed reader reads every Session. The executor reads only on a Decision, scoped by launcher
  grants. This reopens none of the held read-policy decisions; it is a read the operator delegates through Actions
  until `THREAD_READ_POLICY` replaces it.
- **The OAuth route gains a path.** The [agreed route](multiagent_communication.md#oauth-callers) for OAuth callers
  (2026-10-17) sends from an Action box as an ordinary workload. That stays; this adds Actions for what no box can
  do, and every backend still authenticates workload tokens only.
- **Two paths would open a Session.** The app opens Sessions for the operator and the executor for an approved
  caller. They differ in who decided, and both reach the one Dispatcher queue.

## Phases

Each phase is independently useful; P1's parts are separate PRs.

- **P0, configuration only (optional).** Bind `claude-ai` to the `send-to-*` sets the operator picks, so Claude
  can message existing Haku or public-coder Sessions from a box. It also exercises the agent receive, read and
  acknowledge behavior the cross-agent plan still lists as unvalidated.
- **P1, the `orchestration` group under the operator's act.**
  1. The executor kind with `list_launch_presets` and `launch_sandbox`, writing launcher grants; the preset catalog
     rendered into its settings; Sandbox, PolicySet and PolicyBinding `create` for its account.
  2. `open_session` and the four commands; the executor's account in the Dispatcher's callers and wake callers.
  3. `get_session` and `list_sessions` over History's existing Session lookups and the Dispatcher's reads; the
     executor's account among History's readers.

  Acceptance is a k3s system test: a test caller launches (approved by the test), opens, submits and sees the
  Session go idle; a second account's same calls stay on the human path; a recreated same-name Sandbox does not
  match the old grant; deleting the Sandbox removes its grants.

- **P2, reading turns, after `HISTORY_READ_FOLLOW_API`.** `read_turns` and `wait_for_turn` on the reviewed read and
  follow API. Acceptance adds waiting for and reading a turn's outcome, and a wait that spans several calls.
- **P3, backend grants, one operation at a time.** As `THREAD_CREATE_AUTHORIZATION`,
  `DISPATCHER_SEND_AUTHORIZATION_GENERALIZE` and `THREAD_READ_POLICY` land, the executor calls as the caller,
  launcher grants write the backend rule types, and its Dispatcher and History entries go; `send_message` joins the
  group then.

Later and unplanned: auto-approved launches within reviewed bounds (`AGENT_LAUNCH_POLICY_DESIGN`), lifecycle
Actions, an inbox for external callers, subscriptions to a worker's lifecycle ("this Sandbox died"), a fleet view
grouping workers under their launcher, and a spend budget per account at the LLM ingress, which
`THREAD_CREATE_POLICY` records as the operator's actual concern.

## Decisions for the operator

Asked one at a time; each open one has a default the plan assumes.

1. **The operator's act for launch, Session commands and reads in v1. Accepted by the operator, 2026-10-26 23:37
   UTC,** as a transitional path: the `orchestration` group, with the Dispatcher and History trusting its executor
   until each backend rule lands. Rejected for now: building the caller rule types in the Dispatcher and History
   first.
2. **Launcher grants.** Default: a launch requests all of them on the new Sandbox, shown on the card. Alternative:
   none by default, each requested explicitly.
3. **The preset catalog in the executor's settings.** Default: yes, rendered from the same definition as the app's.
   Alternative: launch takes concrete fields with no catalog to read, and the caller learns templates and sets
   elsewhere.
4. **No inbox for the caller in v1.** Default: replies are turn outcomes the caller reads. Alternative: promote
   `EXTERNAL_AGENT_INBOXES` so a worker can reach a caller unprompted.
5. **`claude-ai` as the orchestrating account.** Default: yes, under the pending
   [`CLAUDE_AI_SA`](task_dag.md#claude_ai_sa--review-the-external-callers-actual-authority) review. Alternative: a
   second Connection bound to an account that holds only orchestration grants.
6. **The executor inside the Action Service.** Default: yes. Alternative: its own service, which would then need
   the caller passed to it by Actions, a trust channel that does not exist.

## Alternatives considered

- **The app's API with the `agentplane-agent` token.** The app already resolves presets and opens Sessions, and
  accepts a Kubernetes token for that one account. But an executor calling it makes a backend depend on the app,
  which the dependency rule forbids, and it grows the app's role where the principles want it thinner.
- **An upstream MCP server linked with the operator's identity,** as `kubernetes_admin` is. Actions would mount it
  unchanged, but it never learns the caller, so it can neither bind launcher grants to the caller nor record who
  launched what.
- **MCP edge tools that call each backend with a minted caller token.** The thinnest path for caller-granted
  operations, but only sending has a caller grant today, it offers no approval for the rest, and an in-cluster
  orchestrator would use a different surface.
- **Everything from an Action box.** The box is transport: it can do only what its account may, so it inherits every
  gap above, and each step costs an exec round trip plus JSON quoting inside a shell script.
- **Backend grants first.** The right end state, which P3 reaches; doing it first holds everything on the held read
  policy and on `THREAD_CREATE_POLICY`.
