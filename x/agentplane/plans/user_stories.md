# User stories: the shape Agentplane is growing toward

Status: **north star, written down from Rai's description (2026-09-03).** These are the
experiences the stretch nodes in [`task_dag.md`](task_dag.md) exist for. Each story names what
already stands under it and what is still missing; a story leaves this file when the app delivers
it. The unifying concept across all of them is the **trust tier** of an agent identity: which
credentials it may hold, which data it may receive, which transcripts it may read, and who judges
its channel.

## 1. Ask me, I decide

Rai chats with an agent. Mid-task the agent needs something it does not have: use of a token for
one call, a Kubernetes permission, a command run on Rai's own machine for diagnostics. The ask
reaches Rai as a card wherever Rai is, with the agent's rationale; Rai approves or denies either
the single operation or a standing grant. The agent keeps working meanwhile and learns the
decision as a later input in its thread.

Standing under it:

- The Haku grants system already brokers exactly this for Haku tool calls: `create_grant` with
  exact origins or namespaces plus verbs, a duration and a rationale, manual approval under the
  agent policy, `get_tool_call` for the long poll, `withdraw_tool_call`. The agent working on
  Agentplane lives this story from the agent's side every time it needs a key it does not hold.
- [`async_approvals.md`](async_approvals.md): submission never blocks, no expiry, the decision
  arrives as a thread input, one envelope for every tool, a batcher so five clicks are one input.
- [`external_access.md`](external_access.md): delegated identity where the target's RBAC can
  express the boundary, brokered credential where it cannot, agent-requested grants, and the
  revocation gate (placeholder token, substitution only while the ledger and the apiserver agree).
- Credentialless egress, running on staging: per-Pod sidecar, central proxy holding the
  credentials and the per-identity rules, placeholder tokens the agent cannot use elsewhere
  (<../egress/SPEC.md>). Two halves of the ask are already there — a refused call comes back as
  `403` with a machine-readable reason, and an agent can read which rules it is bound to, so it can
  tell what it lacks from what it was never given.
- A standing grant already has an object to be: an `EgressBinding` the app creates and revokes while
  a sandbox runs, each grant its own binding with its own expiry
  ([`../docs/egress_composition.md`](../docs/egress_composition.md)).
- Host commands: Haku's node daemons (`hostexec`) already run an approved command on a named
  machine.
- Delivery to Rai exists once already: Haku Console sends a Web Push with Approve and Deny for a
  waiting tool call and retracts it when the call leaves the queue
  ([`haku/SPEC.md`](../../../haku/SPEC.md)).
- [`../docs/hooks.md`](../docs/hooks.md): a `PreToolUse` deny with a reason is the cheapest way to
  turn a refused call into an ask the model understands.

Missing:

- **A settled operation/approval contract.** A sandboxed agent's request (this token for this call,
  this verb on this namespace, this command on this host) needs a first-class object the app stores
  and shows, with rationale and exact operation, distinct from the Haku `tool_call` it may become.
  The product noun, lifecycle, pending-turn behavior, MCP wrapping, and authority split are not
  decided yet; see [`operations_and_access.md`](operations_and_access.md). Do not implement an
  Agentplane-wide “ask” object until those gates are resolved.
- **Delivery to Rai.** A notification with approve and deny buttons that answer the operation;
  the app's
  inbox is the fallback view, never the primary one.
- **The decision as an input.** The batcher and the `<agentplane-event>` envelope from
  [`async_approvals.md`](async_approvals.md), delivered by the bridge on the paths the scripted
  tests pin.
- **Per-operation versus standing grant.** One ask, two possible answers. The standing answer is
  an `EgressBinding` or a Kubernetes binding, both of which the app can already mint and revoke;
  the single-operation answer has no mechanism at all, and it is the one an ask usually wants.

## 2. Trusted orchestrator, untrusted fleet

Haku holds Rai's personal data and runs on Anthropic models; it is trusted. Rai wants Haku to
spawn, manage, and delegate to a fleet of Codex agents on OpenAI models, on one condition: the
work Haku hands them involves only public data. Codex agents build generic, non-sensitive things
in public (code, simulations, tooling; `finance/augur` is the model), and Haku slots the private
data in afterwards, for instance to run a personalized simulation. Those agents hold only the
tokens Rai is fine with OpenAI touching. In the full version a judge sits on the channel between
Haku and the fleet and checks that nothing sensitive crosses it.

Standing under it:

- One sandbox per agent with its own Kubernetes identity, standalone Sandboxes, which is
  what the central proxy keys credentials and rules on.
- Trajectories outlive sandboxes (`T1`): every event on both sides of a delegation is stored under
  a thread, so the audit of what crossed the channel is the same store the UI reads.
- The threads API is the seed of cross-transcript reads. Haku Console already scopes conversation
  reads to the reader's trust tier ([`haku/console/TODO.md`](../../../haku/console/TODO.md)
  § Scope conversation reads to the reader's trust tier): the fence is the tier, not the room.

Missing:

- **Tiers on identities.** A sandbox carries the tier of the model provider it talks to, and the
  tier decides the credential set the proxy will substitute (the OpenAI-safe set), the transcripts
  the agent may read, and whether a judge sits on its channel.
- **No delegation primitive.** The orchestrator uses the objects Rai uses: it creates a sandbox
  from a template its identity may use, opens a session, sends an `Input`, and reads the
  session's events. Its prompt tells it which templates and RBAC configurations it may create
  without asking, that doing so is allowed, and how to start and monitor an agent. Nothing
  called a task or a delegation exists; the A2A evaluation
  ([`../docs/a2a.md`](../docs/a2a.md)) stays decided.
- **Reading an agent without drowning.** The events stream carries every native frame and text
  delta; an orchestrator wants a turn's outcome and the assistant's final text. The thin
  addition is a kind filter on the events endpoint and on stored events, so the orchestrator
  subscribes to `TurnCompleted` and completed items only.
- **The judge.** An agent on a trusted model that gets each message crossing a tier boundary and
  decides whether to admit it, answering allow, redact, or block with a reason that goes back
  to the sender as an input; a classifier only if the agent proves too slow or costly. The inbound direction (fleet to Haku) is the prompt-injection
  direction and gets its own, different check. The judge's decisions are trajectory events, so a
  leak that got through is findable.
- **The orchestrator's identity on the API.** Haku creates, suspends, and archives sandboxes under
  its own Kubernetes identity, presenting an audience-scoped token the way the Ducktape agent does
  on staging today. What it may create is a permission on that identity, held wherever the app
  keeps policy; a SandboxTemplate is a Pod shape and carries no permission, because a template
  is copied into each Sandbox at creation and cannot change under a running Pod, while a
  permission on the identity can (Rai).
- **Messaging in any topology.** Agents message each other back and forth through the same
  inputs and events, and the graph is a mesh, not a tree: a specialist may talk to a sibling,
  or back to the orchestrator, under the same tier rules and the same judge on any edge that
  crosses a tier.
- **Subscriptions to lifecycle events.** An agent that spawned another subscribes to what happens
  to it, "this sandbox died" first of all, and gets that as an input; the same delivery path as
  an approval decision, with the sandbox inventory as the source.

## 3. An orchestrator with specialists

Haku decides on its own to spin up specialist agents, delegates parts of a task, waits for or is
woken by their results, and folds them together. Rai sees the fleet: which agent is working on
what, for whom, under which tier, and can step into any thread.

Standing under it: everything in story 2, plus named threads and search (`T2`, `T3`), which is how
"which agent did this, and why" is answered later.

Missing:

- **A fleet view.** Threads grouped by the orchestrator that owns them, with the delegation edges
  visible, in the same app that shows a single session today.
- **Wake-ups.** A specialist's `TurnCompleted` reaching an idle orchestrator as an input, when it
  asked to be woken, is the same delivery path as an approval decision; the batcher makes a burst
  of results one input.
- **A notification queue with a read primitive.** Everything addressed to an agent (a sandbox
  died, a turn completed, a decision landed, a message from another agent) accumulates in a queue
  the agent drains with one call, oldest first, marking what it read; the wake-up input tells an
  idle agent the queue is non-empty, a busy agent reads when it chooses. This is the primitive
  Claude Code gives its own sessions as `ReadNotifications` (Rai).
- **Cross-transcript reads for the orchestrator**, tier-scoped, so Haku can read what a specialist
  did without the specialist being able to read Haku.

## 4. Haku itself lives here

Haku is not a client of Agentplane: it is an agent running in an Agentplane harness, long-lived,
managing Rai's life and doing whatever is useful. Its definition and its memories live in a git
repository, as the current `haku-state` does (the root cards `AGENTS.md`, `SOUL.md`, `MEMORY.md`
and the hubs; [`haku/README.md`](../../../haku/README.md)), so the sandbox is disposable and
Haku is not: a new sandbox clones the repository and resumes.

Standing under it:

- A sandbox whose harness resumes across process restarts and suspend/resume, and whose
  trajectory outlives it (`T1`).
- `haku-state` on Forgejo with tokens minted by the GitOps controller
  ([`tf/gitops/haku-state`](../../../tf/gitops/haku-state)), which is exactly the credential the
  egress proxy substitutes for Haku's identity. The shape is proven: staging's sandboxes reach
  GitHub's API and HTTPS git as `agentydragon-agent` with a PAT they never hold, and the acceptance
  suite checks it against the proxy's own record rather than the agent's account of it.
- Standing instructions on the session (<../runner/SPEC.md>): what a long-lived agent is for reaches
  the model on every turn, including a resumed one, without being written into the harness or into
  the first input. They are fixed for the session's life, which is the strongest promise both
  harnesses can keep — Codex accepts an override on resume and silently ignores it.
- The tier model of story 2: Haku is the private, trusted tier; everything it authors inherits
  that.

Missing:

- **A long-lived session as a first-class thing**: a thread that is never archived, survives
  harness compaction and pin refreshes, and is woken by events rather than only by Rai.
- **Memory writes as git commits** from inside the sandbox, through the proxy, with the same audit
  as any other egress. What is missing is Haku's own credential and rule, not the mechanism: a
  Forgejo `EgressCredential` and a policy that admits the write, where staging has a GitHub one.

## 5. Agentic UI: Haku authors its own affordances

Haku manages a dashboard app Rai interacts with. Some or all interactions wake Haku and let it
act: Rai presses "No, I don't care about this", and Haku dismisses the card and rewrites the
paragraph that led to it. One agent writes the interaction surface it is then driven through.

Standing under it:

- The decision that external events arrive as thread inputs, and the batcher and envelope
  from [`async_approvals.md`](async_approvals.md): a UI event is one more source, delivered as a
  `<agentplane-event>` in a user-message envelope, batched with whatever else arrived.
- Haku already owns a deployed UI: it authors the `haku/ui` repository on Forgejo, the image is
  published from it, and Flux applies the workload under the constrained `haku-state` reconciler
  ([`cluster/k8s/haku/ui-image-webhook`](../../../cluster/k8s/haku/ui-image-webhook/README.md)).
  The page Haku writes is a solved problem; the pipe from the page back to Haku is not.
- Haku-authored workloads keep running under Haku's Kubernetes identity, deployed by Flux from
  `haku-state`, and Haku the agent manipulates them by committing; Agentplane hosts none of it
  and only takes the events the UI posts.

Missing:

- **The event pipe.** Rai clicks; the click reaches `haku-ui`, Haku's own code behind an
  Authentik proxy defined in ducktape; that code decides whether Haku the agent should hear
  about it and posts JSON to an ingress that is Agentplane's code, which batches and rate-limits
  and delivers it to the Haku sandbox's session as an `Input` in an envelope. Haku is allowed to
  write code that gets deployed where it can send messages to Haku; if it built the UI to lie,
  it would be lying to itself. So the UI posts as Haku's Kubernetes identity, the envelope names
  `haku-ui` as the source, and Rai's identity is Authentik's business at the UI's edge, not the
  envelope's. The UI never gets a direct pipe: the ingress is the batcher from
  [`async_approvals.md`](async_approvals.md) with a workload identity on the caller side, feeding
  the existing inputs route: no new object.
- **Rendering state Haku owns**: the cards and paragraphs are data Haku edits, so "dismiss and
  rewrite" is a write to that state followed by the page re-rendering, not a redeploy.

## What this fixes about the order of work

- Story 1 is next: the proxy has landed and is where a denied call turns into an ask, and the
  approvals machinery is already designed.
- Story 2 has the proxy's per-identity credential sets and the trajectory store, and adds tiers,
  the events kind filter, and the judge.
- Story 3 is story 2 with the orchestrator in the driver's seat and the fleet view on top.
- Story 4 is the long-lived thread and memory-in-git on top of a resuming sandbox and `T1`; story 5
  is the event pipe on top of story 4, Haku's existing UI pipeline, and the approvals delivery
  path.

## Open questions

- Whether the judge is a classifier, a judge agent, or both in series, and what "sensitive"
  means as data classes the judge is told about rather than left to infer.
- How the ingress admits a workload: the sandbox identity's own token (TokenReview, as the
  egress proxy does) or a per-workload secret the deploy pipeline mints; the former needs no
  new secret.
- Which notification channel carries the ask cards; the existing Haku console approvals are the
  fallback until one is chosen.
