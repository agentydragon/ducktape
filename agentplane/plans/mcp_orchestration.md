# Orchestrating Agentplane agents from an MCP client

Status: **P1 landed 2026-10-27 as the [`orchestration_v1` Action group](../action_service/orchestration_v1/README.md),
a stopgap that acts for the operator ([ADR](../docs/adr_orchestration_v1.md)).** Turn reads and bounded waits are
implemented in that group's contract. What remains: moving each operation to a grant its backend checks (P3),
and bounding what a launcher may let its workers do.

Claude.ai is the first client, but nothing here is specific to it: the same Actions serve any MCP Connection, and an
orchestrator in a Session Sandbox reaches them through the [`agentplane` CLI](../docs/agent_cli.md)
([user stories 2 and 3](user_stories.md#2-trusted-orchestrator-untrusted-fleet)).

## Shape-matched approval is the v1 expedient

The operator, 2026-10-27 00:03 UTC: auto-approval policies "looking at the structure of the actions to auto-approve
is a little ugly, it would be nice if we had specific rbac-style permissions for the involved actions rather than
auto-approving them by their shape". What a launcher grant's `argument_schema` rule costs:

- **The rule restates the arguments.** It permits what its author predicted the JSON would look like. An Action that
  addresses a worker by `session_id`, or gains a second target argument, falls outside the schema or slips past it,
  and nothing tells the rule's author.
- **It cannot resolve anything.** A schema sees literals: it cannot map a Session to its Sandbox, follow a tag, or
  check a UID against the live object, so every Action carries the Sandbox verbatim and the executor rechecks it.
- **It skips a gate rather than granting a permission.** The authority stays the executor's service identity, and the
  rule means nothing outside the Action Service: the same operation through the Dispatcher, the CLI or a later
  backend path needs a second, differently shaped rule.
- **It reads badly.** The Policies tab shows a JSON Schema, not "`claude-ai` may submit input to Sessions in
  `worker-3f2a`".

The target is a permission check: each Action declares the verb and resource it acts on, and a typed rule grants
verbs over targets, as `session_inbox_access` already does for `send_message` with `operations` and Sandbox
`targets`:

```yaml
- type: session_access # sketch: session_inbox_access with more verbs
  operations: [send_message, open_session, submit_input, interrupt_turn, read_history]
  targets: { sandboxes: [{ name: worker-3f2a, uid: 9b0e… }] }
```

The authorizer resolves a request to caller, verb and Sandbox from the Action's declaration, not from argument text,
and answers "may this account do this to that Sandbox" the same way wherever the operation enters. A launcher grant
becomes that one rule; an Action approval stays an optional human gate on top; `action_auto_approve` keeps its role
as a standing delegation for Actions with no resource model. P3's backend rule types are one verb-over-target family
the backends share, not one bespoke rule each.

Where the check runs (operator, 00:18 UTC: "send message, interrupt, open session might end up looking like rbac
checked on the runner gateway side"): Session commands in the Runner Gateway, whose Dispatcher already authorizes
the caller of every command; history reads in History; inbox messages in Notifications. The subject is the caller's
ServiceAccount and the object is the Sandbox, never a Session (operator, 00:37 UTC: "the thing that's the
object/subject is a SA / sandbox level"). It could be Kubernetes RBAC, with custom verbs such as `open-session`,
`submit-input` and `interrupt` on `sandboxes`, checked with a `SubjectAccessReview` and granted by a Role naming the
worker through a `kubernetes_role_binding` rule: Kubernetes' own check on who may bind what comes with it, but a Role
cannot pin a UID, so the Gateway still compares it. Or it could be a typed `PolicySet` rule over Sandbox targets by
name, UID or tag, as `session_inbox_access` selects by inbox tag.

## What a launcher may let its workers do

Open (operator, 2026-10-27 00:41 UTC: "how to specify what's the set of things that an agent may allow its launched
sandboxes to do"). In v1 the operator approves every launch and sees the template, policy sets and tags it names, so
the launch card is the bound. Once a launch can be auto-approved (`AGENT_LAUNCH_POLICY_DESIGN`,
`AGENT_SANDBOX_LAUNCH`), each launching account needs that bound written down: which templates, which PolicySets it
may attach, which inbox and history tags it may stamp, and how many workers it may run at once. Part of the question
is whether the bound is a subset of the launcher's own grants or a list of its own; nothing is inherited, which
suggests its own list, so an orchestrator with no GitHub access could still start a worker holding
`common-github-read`. Kubernetes RBAC has this shape in its `bind` verb, where binding a Role takes that Role's
permissions or `bind` on it by name; the counterpart here would grant attaching named PolicySets.

## Phases

- **P0, configuration only (optional).** Bind `claude-ai` to the `send-to-*` sets the operator picks, so Claude
  can message existing Haku or public-coder Sessions from a box. It also exercises the agent receive, read and
  acknowledge behavior the cross-agent plan still lists as unvalidated.
- **P3, backend grants, one operation at a time** (`ORCHESTRATION_V1_BACKEND_GRANTS`). As
  `THREAD_CREATE_AUTHORIZATION`, `DISPATCHER_SEND_AUTHORIZATION_GENERALIZE`, `THREAD_READ_POLICY` and
  `AGENT_SANDBOX_LAUNCH` land, the executor calls as the caller with a token for its account minted at execution,
  launcher grants write the backend rule types, and the Action Service leaves the Dispatcher's callers and History's
  readers; `send_message` joins the group then. The tool names and arguments stay.

Later and unplanned: auto-approved launches within reviewed bounds (`AGENT_LAUNCH_POLICY_DESIGN`), lifecycle
Actions (suspend, resume, delete), an inbox for external callers, subscriptions to a worker's lifecycle ("this
Sandbox died"), a fleet view grouping workers under their launcher, one preset owner the app and the group both read
at run time instead of two rendered copies (operator, 2026-10-27 00:51 UTC), and a spend budget per account at the
LLM ingress, which `THREAD_CREATE_POLICY` records as the operator's actual concern.
