# Orchestration Actions (v1)

**A stopgap, not the long-term design.** It fills the immediate need for an agent to start and
drive other agents, and it has to work, but its authority model is the one to replace: the Action
Service acts with its own account where each backend should check the caller, and launcher grants
match argument shapes where they should be typed permissions on the Sandbox. The `_v1` in its name
leaves room for that replacement.

An Action group that lets a caller launch Session Sandboxes and drive their Sessions: an MCP client
such as claude.ai through the MCP edge, or an in-cluster agent with `agentplane actions`. Its
Actions are `list_launch_presets`, `launch_sandbox`, `open_session`, `submit_input`,
`interrupt_turn`, `change_model` and `change_reasoning_effort`.

## The contract

**The Decision is the authority.** The executor acts with the Action Service's own account: it
creates Sandboxes, PolicySets and PolicyBindings, and the Session Dispatcher trusts it as a caller,
as it trusts the app acting for its operator. A request reaches that only once it is decided, by the
operator or by an `action_auto_approve` rule the operator wrote, so each Action is an act the
operator delegates. No backend checks the caller itself yet.

**Launcher grants.** A launch names what its caller may then do to the new Sandbox without asking
again: `send_message` to its Session inboxes, and any of this group's Actions that take the Sandbox.
The executor writes them as a PolicySet and a PolicyBinding named `<sandbox>.launcher`, both owned
by the Sandbox so that deleting it revokes them. The binding's subject is
`ExecutionRequest.caller`, the authenticated account that submitted the launch; no argument names
it. Approving a launch approves its grants, and a launch that names none gets none.

**One incarnation.** Every Action on a Sandbox names it by name and UID, and a grant matches only
that UID. The executor rereads the Sandbox before each call and refuses another incarnation, since
`OpenSession` takes the Sandbox by name alone.

**Concrete fields.** A launch names its template, PolicySets, inbox tags and bootstrap, never a
preset: what the operator approves is what runs. `list_launch_presets` answers with the reviewed
presets as those fields, rendered into this group's settings from the definition the app's presets
come from, for a caller to copy.

**Retries.** A repeated launch by the same caller with the same arguments adopts its Sandbox and
completes its grants, so a launch whose attempt ended without an outcome can be submitted again;
any other launch of a taken name is refused. `open_session` and each command carry a caller-minted
`session_id` or `command_id`, which the Dispatcher deduplicates.

A launched Sandbox's own account is bound to this deployment's default PolicySets and the ones the
launch names. A launch may not name a launcher grant's PolicySet, which would hand another
Sandbox's launcher grant to the new one.
