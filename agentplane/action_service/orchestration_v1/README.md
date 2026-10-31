# Orchestration Actions (v1)

**A stopgap, not the long-term design.** It fills the immediate need for an agent to start and
drive other agents, and it has to work, but its authority model is the one to replace: the Action
Service acts with its own account where each backend should check the caller, and launcher grants
match argument shapes where they should be typed permissions on the Sandbox. The `_v1` in its name
leaves room for that replacement. Why it is built this way: the
[ADR](../../docs/adr_orchestration_v1.md).

An Action group that lets a caller launch Session Sandboxes and drive their Sessions: an MCP client
such as claude.ai through the MCP edge, or an in-cluster agent with `agentplane actions`. Its
Actions are `list_launch_presets`, `launch_sandbox`, `open_session`, `submit_input`,
`interrupt_turn`, `change_model`, `change_reasoning_effort`, `get_session`, `list_sessions`, `read_turns` and `wait_for_turn`.

## The contract

**The Decision is the authority.** The executor acts with the Action Service's own account: it
creates Sandboxes, PolicySets and PolicyBindings, and the Session Dispatcher and the History Service
trust it as a caller, as they trust the app acting for its operator. A request reaches that only
once it is decided, by the operator or by an `action_auto_approve` rule the operator wrote, so each
Action is an act the operator delegates. No backend checks the caller itself yet.

**Launcher grants.** A launch names what its caller may then do to the new Sandbox without asking
again: `send_message` to its Session inboxes, and any of this group's Actions that take the Sandbox.
The executor writes them as a PolicySet and a PolicyBinding named `<sandbox>.launcher`, both owned
by the Sandbox so that deleting it revokes them. The binding's subject is
`ExecutionRequest.caller`, the authenticated account that submitted the launch; no argument names
it. Approving a launch approves its grants, and a launch that names none gets none.

**One incarnation.** Every Action on a Sandbox names it by name and UID, and a grant matches only
that UID. Before each command and Session read, the executor rereads the Sandbox and refuses
another incarnation. Openings and commands pass that expected UID to the Dispatcher, which checks
the current Sandbox UID before accepting them. `get_session`, `read_turns` and `wait_for_turn` answer only
for a Session that its opening or its history ties to that UID and neither ties to another;
`list_sessions` finds Sessions by the UID alone, so it still lists them once the Sandbox is deleted.

**Concrete fields.** A launch names its template, PolicySets, inbox tags and bootstrap, never a
preset: what the operator approves is what runs. `list_launch_presets` answers with the reviewed
presets as those fields, rendered into this group's settings from the definition the app's presets
come from, for a caller to copy.

**Retries.** A repeated launch by the same caller with the same arguments adopts its Sandbox and
completes its grants, so a launch whose attempt ended without an outcome can be submitted again;
any other launch of a taken name is refused. `open_session` and each command carry a caller-minted
`session_id` or `command_id`, which the Dispatcher deduplicates.

`open_session` accepts `HARNESS_CENTRAL_SAMPLER`. The Dispatcher selects its sampler service
from deployment configuration; Actions and launch presets do not accept routing fields.

A launched Sandbox's own account is bound to this deployment's default PolicySets and the ones the
launch names. A launch may not name a launcher grant's PolicySet, which would hand another
Sandbox's launcher grant to the new one.

## Reading and waiting for turns

`read_turns` takes `sandbox` (name and UID), `session_id` and `count` (1–100). It reads
History's `FollowSession` from the requested number of retained `TURN_STARTED` entries back,
filtering confirmed user messages, turn starts/completions and item starts/completions. It returns
`turns` oldest first and an opaque `resume_token` for that Session and Sandbox incarnation.
Each turn includes `turn_id`, confirmed `input` objects (`text` and `command_ids`),
`tool_calls` (`item_id` and `name`),
optional terminal `status`/`error`, and optional `final_assistant_message`: the last completed
assistant-text item. Reasoning and tool output never become that final message. A running turn has
no terminal status. A removed range makes the read fail rather than return an apparently complete
transcript. Each input's `command_ids` comes from History's `origin_command_ids`; an input with no
originating command has an empty array. Replayed confirmations of the same native message within a
turn do not duplicate inputs or command IDs. Distinct messages with equal text remain distinct.

`get_session` keeps the Dispatcher's `state`, `reason` and timestamps on each of its at most 20
recent dispatches. A separate optional `history` object reports the latest observed History fact:
`outcome: "confirmed"` with `turn_id`, `"applied"` for a model or reasoning-effort change,
`"failed"` with `reason`, or `"no_op"` with `reason`. Admission alone is never a History effect.
An absent `history` field means no fact was observed in this read, including when History has not
reached that dispatch; it does not assert that the command had no effect.

After checking the Sandbox incarnation, `get_session` uses one non-following, kind-filtered
`FollowSession` inside the deployment's read budget. It starts just before the earliest listed
journal admission, capped at the summary's committed cursor to tolerate ingest lag. If any listed
admission is unknown, it starts at zero. Before History knows the Session it returns no History
facts. Read failures, required reconnects and removed ranges fail the Action rather than produce
an apparently complete result. `get_session` shares the `direct_wait_seconds` minus one second
read budget with the turn reads; the caller's earlier deadline also applies.

`wait_for_turn` takes the same Sandbox and Session plus `resume_token` and `wait_seconds`
(positive, at most 30). It follows only turn completions from that cursor. The executor bounds the
whole read, including incarnation checks, to the deployment's `direct_wait_seconds` minus one
second for result publication. After the incarnation checks, the follow is bounded by the requested
wait and the remaining read budget. A deployment with a budget of at most one second refuses turn
reads. The caller's earlier deadline also applies.

The result is `completed: true` with `turn` (`turn_id`, `status`, optional `error`) for the first
completion after the token, or `completed: false` when the wait deadline expires and the caller
has not cancelled or exhausted its own deadline. A History `DeadlineExceeded` before the wait
deadline fails the Action. Both results carry a new `resume_token`. Repeat with it to continue
without missing or repeating completions, then use
`read_turns` for full messages. A completion token stops at that entry even if its History frame
contains later completions. Planned History renewal reconnects internally from the latest progress;
a bare stream EOF or backend failure fails the Action. Tokens encode progress, confer no authority,
and do not bypass the incarnation check.

Both Actions are launcher-grantable. A launcher grant names the Sandbox incarnation; the executor
also ties the Session's opening/history to that UID before calling `FollowSession`.

## Deployment

An environment offers the group with `ActionsProps.orchestration_v1`
(`cluster/cdk8s/agentplane/environment.py`). Its Action Service then:

- has the group in its settings, with the app's presets and default PolicySets, and the
  `session_dispatcher` and `history_service` it calls with projected tokens of their audiences;
- is a caller and waker of the Session Dispatcher and a reader of the History Service;
- may get and create Sandboxes, PolicySets and PolicyBindings in its namespace;
- creates those only in the executor's shape, which a ValidatingAdmissionPolicy enforces: a
  Sandbox bound to no launcher grant, and a grant owned by one Sandbox, named for it, whose rules
  name only that incarnation and only this group's Actions. The policy cannot see who called the
  launch, so it bounds what a grant reaches, not who holds it.

Offer new direct tools only after the Action Service and History images implementing them are pinned
in the environment. Staging's `direct_tools` addition is a separate configuration change.
