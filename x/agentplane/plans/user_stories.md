# User stories: the shape Agentplane is growing toward

Status: **north star, written down from Rai's description (2026-09-03).** These are the
experiences the stretch nodes in [`task_dag.md`](task_dag.md) exist for. Each story names what
already stands under it and what is still missing; a story leaves this file when the app delivers
it. The unifying concept across all three is the **trust tier** of an agent identity: which
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
  Agentplane is living this story from the agent's side: a grant to read one staging key has been
  pending for hours while the work continued.
- [`async_approvals.md`](async_approvals.md): submission never blocks, no expiry, the decision
  arrives as a thread input, one envelope for every tool, a batcher so five clicks are one input.
- [`external_access.md`](external_access.md): delegated identity where the target's RBAC can
  express the boundary, brokered credential where it cannot, agent-requested grants, and the
  revocation gate (placeholder token, substitution only while the ledger and the apiserver agree).
- The decided egress shape (`J`): per-Pod sidecar, central proxy holding the credentials and the
  per-identity rules, placeholder tokens the agent cannot use elsewhere.
- Host commands: Haku's node daemons (`hostexec`) already run an approved command on a named
  machine.
- [`../docs/hooks.md`](../docs/hooks.md): a `PreToolUse` deny with a reason is the cheapest way to
  turn a refused call into an ask the model understands.

Missing:

- **The ask itself, in Agentplane's own vocabulary.** A sandboxed agent's request (this token for
  this call, this verb on this namespace, this command on this host) as a first-class object the
  app stores and shows, with the rationale and the exact operation, distinct from the Haku tool
  call it may become.
- **Delivery to Rai.** A notification with approve and deny buttons that answer the ask; the app's
  inbox is the fallback view, never the primary one.
- **The decision as an input.** The batcher and the `<agentplane-event>` envelope from
  [`async_approvals.md`](async_approvals.md), delivered by the bridge on the paths the scripted
  tests pin.
- **Per-operation versus standing grant.** One ask, two possible answers; a standing grant is a
  rule in the central proxy or a Kubernetes binding, minted and revoked through the ledger.

## 2. Trusted orchestrator, untrusted fleet

Haku holds Rai's personal data and runs on Anthropic models; it is trusted. Rai wants Haku to
spawn, manage, and delegate to a fleet of Codex agents on OpenAI models, on one condition: the
work Haku hands them involves only public data. Codex agents build generic, non-sensitive things
in public (code, simulations, tooling; `finance/augur` is the model), and Haku slots the private
data in afterwards, for instance to run a personalized simulation. Those agents hold only the
tokens Rai is fine with OpenAI touching. In the full version a judge sits on the channel between
Haku and the fleet and checks that nothing sensitive crosses it.

Standing under it:

- One sandbox per agent with its own Kubernetes identity (`I3`, standalone Sandboxes), which is
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
- **Delegation as a thread-to-thread object.** Haku hands over a task; Agentplane opens a thread
  in a fleet sandbox, carries the task description as its first input, and returns results and
  questions to Haku's thread as inputs. Both directions are ordinary inputs on the trajectories, so
  nothing new is needed to store or replay them. The A2A evaluation ([`a2a.md`](a2a.md)) stays
  decided: the object is ours, not an A2A task.
- **The judge.** A classifier or a judge agent on a trusted model, reading every message that
  crosses from a higher tier to a lower one, answering allow, redact, or block with a reason that
  goes back to the sender as an input. The inbound direction (fleet to Haku) is the prompt-injection
  direction and gets its own, different check. The judge's decisions are trajectory events, so a
  leak that got through is findable.
- **Fleet lifecycle by an agent.** Haku creates, suspends, and archives fleet sandboxes through the
  Agentplane API under its own identity; the agent RBAC that lets the Ducktape agent drive staging
  is the same shape, minus the operator's own credential.

## 3. An orchestrator with specialists

Haku decides on its own to spin up specialist agents, delegates parts of a task, waits for or is
woken by their results, and folds them together. Rai sees the fleet: which agent is working on
what, for whom, under which tier, and can step into any thread.

Standing under it: everything in story 2, plus named threads and search (`T2`, `T3`), which is how
"which agent did this, and why" is answered later.

Missing:

- **A fleet view.** Threads grouped by the orchestrator that owns them, with the delegation edges
  visible, in the same app that shows a single session today.
- **Wake-ups.** A specialist's result reaching the orchestrator while it is idle is the same
  delivery path as an approval decision; the batcher makes a burst of results one input.
- **Cross-transcript reads for the orchestrator**, tier-scoped, so Haku can read what a specialist
  did without the specialist being able to read Haku.

## What this fixes about the order of work

- Story 1 is next after the egress proxy lands: the proxy is where a denied call turns into an
  ask, and the approvals machinery is already designed.
- Story 2 needs the proxy's per-identity credential sets and the trajectory store, both in flight,
  and adds the delegation object, tiers, and the judge.
- Story 3 is story 2 with the orchestrator in the driver's seat and the fleet view on top.

## Open questions

- Whether the judge is a classifier, a judge agent, or both in series, and what "sensitive"
  means as data classes the judge is told about rather than left to infer.
- Whether Haku reaches the fleet as a client of the Agentplane API or whether Haku itself runs in
  an Agentplane sandbox; the stories work either way, the identity model does not need to know.
- Which notification channel carries the ask cards; the existing Haku console approvals are the
  fallback until one is chosen.
