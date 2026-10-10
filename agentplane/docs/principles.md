# Agentplane design principles

These are the commitments new Agentplane design is checked against. Each states the rule, what it
means in practice, and the question a proposal has to answer. A design that breaks one needs an
explicit reason recorded in its design doc, not a silent exception.

## Composable services, not a platform you sign up for whole

Agentplane is a set of separately deployable services, each useful without the others. Running an
agent does not require running approvals; using approvals does not require running an agent.

- The Action Service on its own is an MCP aggregator with operator approvals and auto-approval
  policies. An external product (Claude.ai today) connects to its `/mcp` endpoint over OAuth and
  never touches a Sandbox or runner.
- A runner session reaches Actions only when it asks for one; nothing in the runner requires the
  Action Service to be deployed.
- The egress proxy fronts workloads that are not Agentplane sandboxes at all (for example the
  public coder's OpenClaw).
- The integration app is a client of the backends, never their dependency
  ([dependency rule](service_boundaries.md)).

Composability is among services on Kubernetes, which is a shared baseline rather than an optional
piece.

The integration app exists to serve people: one frontend over Actions, sandboxes, threads and the
rest. Its ideal form is a thin facade in front of N small services that serves that frontend and
owns no backend state. State it holds today, such as the thread archive, is a candidate to move
into a dedicated service, not a pattern to extend.

**Question for a proposal:** can someone who wants only this capability deploy it without the
rest? A new hard dependency between services needs a reason stronger than convenience. Does new
state belong in the app, or in a service the app fronts?

## Separate concepts, mixed and matched; no opaque bundles

Agentplane does not have a monolithic "agent" object that silently decides identity, permissions,
network reach and approvals together. Each concern is its own visible object, and a configuration
is a combination of them:

- identity: a Kubernetes ServiceAccount;
- what may run without a human: `ActionPolicySet`/`ActionPolicyBinding` ([action policies](action_policies.md));
- network reach and credentials: `EgressPolicy`/`EgressBinding` ([egress](../egress/SPEC.md));
- cluster permissions: ordinary RBAC bindings;
- execution environment: `SandboxTemplate`; harness and model per session.

A launch preset pre-fills these choices and carries no authority of its own: runtime services
receive the resolved concrete configuration and never see a preset name ([launch presets](launch_presets.md)).

Bundles are welcome as applications on top of this base. Launching a coder agent with hundreds of
preconfigured choices in one click is a feature, and so would be named access levels or profiles.
What the base must not do is make such a bundle the only supported way to configure a session.

**Question for a proposal:** if it introduces a name that stands for several of these at once (a
"profile", "tier" or "role"), is it an application built from the parts, or does the base start
to require it? Can a user still see and change each part separately, and does any authority attach
to the name rather than to the parts?

## Keep options open; avoid hard-to-reverse choices

Prefer the design that leaves the most choices available later, and be wary of one that is hard
to undo once it lands. A convenient abstraction that bakes a decision into the contract costs
more than it saves when the decision turns out wrong.

For example, an "agent of type X" is only an integration-app preset over the separate parts above.
A one-off session with unusual permissions is therefore just a different combination chosen at
launch. Nobody has to mint a one-off agent type for it, and the backends never learn type names
they would later have to keep honoring.

This is a lesson from Haku Console, Agentplane's predecessor. It bundled its parts into big
concepts too early and became unmaintainable. Agentplane may still, once the use cases are better
understood, decide that a single concept such as "agent" or "trust tier" is the right shape. That
call is deferred as long as possible, and until then bundles live where they are cheapest to pull
out by the roots: as presets in the integration app, not in backend contracts.

**Question for a proposal:** which future options does it close, and how expensive is it to back
out of? If it is hard to reverse, is that cost stated and accepted?

## Keep the information; make dropping it a decision

What crossed a boundary is retained as it crossed, so a later reader can see what actually
happened rather than a summary of it.

The boundary where this matters most is **harness ↔ runner**. Claude Code and Codex are
third-party binaries we do not control, so when one misbehaves the exact frames it sent and
received are the evidence. The runner journals those frames verbatim in both directions, and
derived events cite the native frames they came from ([runner spec](../runner/SPEC.md)). A weird
bug's frames can then be pulled straight out of the log and turned into a harness ↔ runner
behavior test ([scripted harness tests](../harness_tests/README.md)). Any change that trims or
reshapes payloads must keep this boundary verbatim.

Elsewhere:

- The Action Service keeps an append-only event history per request, the policy versions behind
  each Decision, and the upstream `CallToolResult` as returned.
- The LLM ingress forwards provider-native request and response bodies untranslated.
- Projections for display may compact, but only what can be rebuilt from retained evidence.

Preserving is the default; storage is mostly cheap. Dropping data happens only as a deliberate,
design-level decision, such as compacting streamed deltas once a turn completes, never as a side
effect of a convenient schema.

Errors follow the same rule. There is no `except: return "something went wrong"`: a failure
propagates with its cause, or is logged with its detail where a fallback is genuinely correct
([`STYLE.md`](../../STYLE.md) § Exceptions). A failed Thread, Action or request says what failed and
why.

**Question for a proposal:** what does it discard or summarize, and who decided that? If it fails,
what will the operator see?

## Components roll and fail independently

Restarting or breaking one service affects only the work that actually needs it right now.

- Each service owns its durable state and recovers from it: the runner reopens its journal on the
  sandbox's volume; the Action Service resumes dispatches by lease and marks lost work
  `execution_unknown` rather than guessing ([executor liveness](executor_liveness.md)).
- A running agent keeps working while the app, or a service it is not currently calling, restarts.
  When the notification service has crashed, agents saw its errors, noted that it was having
  trouble, and carried on with their work.
- Backend paths are accepted with the integration app unavailable.

**Question for a proposal:** while this component is down or rolling, what else stops? Anything
that does not need it should not.

## Strong outer sandbox, free agent inside it

Agents do not hold real credentials. Inside their sandbox they are otherwise unrestricted.

- The sandbox receives placeholders (`agentplane-credential-<name>`); the egress proxy authenticates
  the Pod-bound workload token and substitutes the real credential per request
  ([ADR](adr_sandbox_proxy_gateway.md), [workload authentication](workload_authentication.md)). The
  LLM ingress holds the model-provider key.
- Within the sandbox the harness runs without per-command approval: Claude Code's `can_use_tool`
  is answered allow, Codex runs with `approval_policy: never`.
- Agentplane never parses the shell commands an agent runs, and never maintains allowlists of
  "safe" binaries or flags. Such semi-permeable filters are brittle and give a false sense of
  containment. The boundary is the Pod today, with KubeVirt VMs as the stronger option; control is
  at what leaves it: egress policy, Actions, RBAC.

The sandbox also protects the agent from itself. Claude Code and Codex assume the agent runs
commands on the same machine as the harness; they do not split into "harness in one Pod, commands
in another". So the harness and the agent's work share a sandbox, and the agent should be free to
run memory-hungry work without being able to kill its own harness by accident. Constraints that
keep the harness alive (resource partitioning inside a VM, for example) serve that freedom; they
are not command policing.

**Question for a proposal:** does it put a real secret where agent code can read it, or does it
try to police what happens inside the sandbox instead of at its boundary?

## Agent-facing affordances are scriptable APIs

What Agentplane offers an agent is an API the agent can call from its own shell and programs: it
can query a subfield, pipe results through `jq`, loop, and write scripts against it, instead of
spending a model turn per tool call. This is the Agentplane-level counterpart of harness modes
that let an agent script MCP tools from code.

- Egress rules, Actions, policies and notification inboxes are HTTP APIs with OpenAPI schemas,
  reached through the egress proxy with the workload placeholder; the platform instructions point
  agents at them ([`agent_instructions.j2`](../sandbox_service/agent_instructions.j2)).
- Kubernetes access is the agent's own `kubectl`, not a wrapper tool.
- MCP is an additional surface, not the only one: the Action Service also serves its operations as
  MCP tools over OAuth.

**Question for a proposal:** can an agent drive this from a script, with a documented schema?

## Any MCP-capable product, no lock-in

Agentplane's approvals and auto-approvals work for any LLM product that can talk to an MCP server,
whoever makes it. The Action Service's `/mcp` endpoint uses standard MCP with OAuth discovery and
dynamic client registration; an external Connection gets the same policies and operator review as
a workload in a sandbox.

For agents Agentplane hosts itself, the runner drives native harnesses (Claude Code and Codex)
through their own protocols, and model calls go through LiteLLM, so a harness is not tied to its
vendor's models (Ollama-served models run under both).

**Question for a proposal:** does it work only for one vendor's client, harness or private
protocol? If so, is that confined to an adapter rather than the contract?
