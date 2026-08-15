# Information trust tiers — which provider may see what, and who enforces it

**Status: design sketch, nothing built.** Operator, 2026-08-15: several agents operating under
this setup, at different information trust levels, with a local classifier guarding the channels
between them, and the high-trust agent delegating unsensitive work down. This records the shape
and the hard parts. It is deliberately not a build order — two of the load-bearing decisions are
values calls (Open questions), and one of the mechanisms is in the wrong place as first proposed.

Companion to <multi_agent.md>, which owns **which** zones, providers and workloads exist. This
owns **what may flow between them**. It takes over the role that document's dangling
`capability_dispatch.md` reference described.

## The reframing that makes this tractable

The ask reads "agents at different trust levels", but the agent is not where the trust lives.
**Everything in an agent's context reaches its model provider.** The tier is a property of the
_provider_; the agent is the vehicle that carries data to one. Two consequences:

- **The thing to label is the corpus, not the message.** `haku-state` is Anthropic-only because
  it is the personal-data motherlode; ducktape is public; k8s diagnostics is neither. A label on
  a corpus is durable and checkable.
- **The enforcement points already exist, per zone**: which workspace is mounted, which
  credential is reflected in, which LiteLLM route the harness can reach, which egress the
  perimeter permits. Four perimeter controls, all of them the kind doctrine already prefers.

That is enforceable. "Classify each message before it crosses" is not, by itself — it is a
probabilistic filter on output from an author assumed adversarial. Both are wanted; only the
first is the boundary.

## An agent kind is its workspace repo

Operator, 2026-08-15, and it is the right mechanism: **agent kinds are distinguished by which
repo they clone as their workspace.** That makes the corpus label above operational rather than
notional — the tier of an agent is a fact about its manifest, checkable from outside, not a
judgment about its behaviour. It also composes with everything this deployment already has per
kind: a SandboxTemplate and claim, a ServiceAccount and RBAC, an egress perimeter, a LiteLLM
route. The workspace clone is already how the Haku sandbox bootstraps, so this is the existing
mechanism carrying one more meaning rather than a new one.

Two things it does not do, and both matter:

- **A repo is a label only while its contents are homogeneous.** The moment a lower tier needs to
  touch part of a higher-tier repo, the answer is to split the repo, not to subdivide the label —
  which is exactly what multi_agent.md already anticipates in wanting `ui/` out of `haku-state`
  before frontend chores can be offloaded. Expect the tier boundary to force repo splits, and
  treat that as the design working rather than as friction.
- **The workspace is not the only inflow.** MCP tool results, room history, and anything another
  agent says all land in context regardless of which repo was cloned. So the repo bounds the
  **starting** corpus, not the accumulated one — which is precisely why the channels between
  agents need their own check, and why that check cannot be inferred from the mount.

## What this inherits from doctrine, including the uncomfortable parts

<../docs/security.md> already states the test, and it applies here unchanged: **every new write
path is evaluated against its destination's trust independently of the egress allowlist.** A
lower-tier agent _is_ a destination, and handing it context is a write path to another company.
So "may Haku delegate this?" is the same question as "may this land in a public CI artifact?",
asked of a different endpoint. This design adds a destination class; it does not need a new
principle.

Two inheritances that constrain the classifier specifically:

- **Haku is assumed adversarial**, so the delegating agent is an untrusted author of the
  delegation payload. The classifier is not reviewing a trusted party's summary — it is reviewing
  output from something a prompt injection may be steering. That is the strongest argument for
  putting the check outside the agent, and equally the reason it cannot be the only control: an
  author who can observe the classifier's behaviour can iterate against it.
- **Enforcement at the perimeter, never by in-agent rules.** An LLM classifier is in-band and
  probabilistic — structurally closer to an in-agent rule than to a perimeter. It belongs as
  defense in depth on the one channel that must cross, not as the thing that makes crossing safe.

**So the primary control is never "the classifier approved it".** It is that the lower tier never
had the higher corpus mounted, never held its credential, and cannot reach it over the network.

## The classifier has to run locally, and that is structural rather than frugal

The archived dispatch plane ran an **Anthropic** classifier ahead of dispatch to a cheaper zone.
That was coherent only because Anthropic sat at the top of a total order: showing the content to
the gatekeeper disclosed it to a party already permitted to see everything.

The moment the tiers are peers with different allowlists — Anthropic broad, OpenAI
whitelist-based — that stops working. A classifier hosted at either has already disclosed the
content to it before deciding whether it may. **Ollama on the operator's own GPUs is the only
placement that does not defeat the check.** Recorded because "just use the good model for the
classifier" is the obvious suggestion, and it is wrong for a reason that is easy to miss.

What that costs, stated up front:

- A small local model is the weakest link in the chain guarding the strongest secrets, doing an
  adversarial task. Calibrate on refusal precision and prefer failing closed.
- **Model residency is already a known problem here.** <../x/dispatch/local_dispatch_zone.md>'s
  scheduler exists because running agents across several models thrashes residency on the GPUs. A
  synchronous classifier in the delegation path is one more resident model competing for the same
  VRAM — and one that must answer **now**, not when a slot frees.

## What the classifier is actually for

Operator, 2026-08-15, and it corrects the framing this document first had. The threat is **not
primarily a malicious lower tier extracting secrets**. It is the **high-trust agent volunteering
something that should never have reached a room a lower-tier agent is sitting in** — Haku
mentioning the operator's embarrassing medical condition while coordinating work, in a channel
where an OpenAI-backed agent is a member and therefore its provider sees every word.

That is a different and harder problem than gating a delegation payload, in three ways:

- **It is per message, not per handoff.** Every message the high-trust agent posts into a shared
  room is a potential disclosure, including ones that are not delegating anything.
- **It is inadvertence, not attack.** The failure is a helpful agent supplying context, so the
  classifier is catching ordinary behaviour rather than an adversary. That makes it more likely to
  fire and more valuable — and it does **not** retire the adversarial case, since the threat model
  still assumes Haku can be steered.
- **The judgment is relevance versus disclosure, not deny-all.** The lower tier genuinely needs
  some context to do its job, so the classifier cannot simply refuse anything personal.

Three channels follow, and they want different mechanisms:

| Channel                   | What crosses                  | Right mechanism                                     |
| ------------------------- | ----------------------------- | --------------------------------------------------- |
| Message into a mixed room | anything the higher tier says | local classifier, synchronous, fail closed          |
| A lower tier's output     | its results and its messages  | an **integrity** control, not a confidentiality one |
| Git push                  | content leaving into a repo   | the destination, **not** the proxy — see below      |

The asymmetry is worth naming: outbound is a disclosure control, inbound is an injection control.
multi_agent.md already says sensor output is untrusted input to Haku; another agent's messages are
the same thing, and need no classifier — they need the treatment every adversarial source already
gets.

**The choke point already exists, which is what makes this enforceable.** The console holds the
only Matrix credential and posts everything the agent says (R11.1 auto-forward — the agent has no
send tool). So the classifier is a filter in the reply path, before the room send, in reviewed
console code that the agent cannot reach around. That satisfies "enforcement at the perimeter,
never by in-agent rules" for a check that is otherwise in-band.

**Make the channel narrow so the classifier's job is easy.** Free prose into a mixed room is the
hardest possible input for a small local model — paraphrase, inference chains and oblique
reference ("the thing from Tuesday") all defeat keyword-shaped judgment. A structured task
envelope, where the higher tier states goal, inputs and acceptance criteria in fields rather than
narrating, is dramatically more checkable and forces the agent to be deliberate about what it is
handing over. Reserve free-form messages for same-tier rooms.

### Git content: check at the destination

The proposal is to classify pushed git content if we drive our own HTTP proxies. **The proxy is
the wrong layer**, and it is worth saying why before someone builds it. `haku-egress-proxy` today
rewrites a header for a known host — cheap, because it inspects request _metadata_. Classifying
pushed content means parsing git's smart-HTTP protocol and unpacking packfiles inside the proxy,
on a path that has to stay fast, for payloads that are large, compressed and delta-encoded.

The destination already has the content unpacked, and two mechanisms fit:

- A **Forgejo pre-receive hook** — synchronous, and refuses the push, which is the semantics
  actually wanted.
- A **required CI status** on PRs — multi_agent.md already lists a PII check as exactly this,
  deferred. Weaker, since the content has already landed on a branch, but it needs no protocol
  parsing at all.

The _other_ half of the idea is right about the proxy, though: **which repos an agent may push to
at all** is metadata, the proxy already sees the request path, and <../TODO.md> already wants it —
that turns a standing all-or-nothing grant into a reviewable list.

## Shared rooms, with the operator in them

The chosen shape (operator, 2026-08-15): agents talk to each other in Matrix rooms the operator
is also a member of, and messages pass the classifier on the way in. This section is what that
requires — the objections that mattered are absorbed as requirements rather than as reasons not
to.

**The policy rule is the room's floor.** A message is checked against the **lowest tier among the
room's current members**, so membership _is_ the policy and there is one number to reason about
per room. It also means adding a member re-parameterizes every future message in that room.

Five requirements follow, in the order they will bite:

- **`m.room.history_visibility` must be `joined`, set at creation.** This is the trap. The
  default is `shared`, under which a newly joined member can backfill the room's entire history —
  every message written before it arrived, none of which was ever classified against its
  presence. Adding an OpenAI-backed agent to an existing room would hand its provider the whole
  backlog in one go, and changing the setting afterwards does not retroactively hide anything. Get
  this right when the room is made, or not at all.
- **A room's tier is monotonically non-increasing.** Everything already said stays disclosed to
  everyone who could read it; removing a low-tier member does not un-disclose. So a room can be
  downgraded once and never restored, and "just add the cheap agent to this existing room" is the
  gesture that spends the room permanently. Prefer creating a room at the tier it will need.
- **Operator presence is detection, not prevention.** By the time the operator reads a leak, the
  event has federated to every member's homeserver and its provider has already seen it. That is
  the argument for the classifier being synchronous and fail-closed rather than an audit log —
  and equally the argument for keeping the operator in the room anyway, because their reaction is
  the only calibration signal the classifier will ever get.
- **A withheld message has to be visible as withheld.** Silently dropping one leaves the operator
  unable to tell whether the agent answered, and leaves the other agents waiting on a reply that
  is never coming. It wants its own `RoomEventKind` in the tag vocabulary
  (<../console/x/matrix_client.py>) so the room shows that something was said and held.
- **Loop protection stops being optional.** matrix_chat_runtime puts mention gating, sender
  allowlists and multi-bot loop protection out of scope explicitly **because it is a DM** (R3.5).
  Several bots in a room reinstates all three, and R1.5's "ignore my own sender" is no longer
  enough — each agent must ignore other agents unless addressed. The dangerous one is two agents
  answering each other: the operator being a **member** does not mean the operator is **watching**,
  so this needs a per-room turn and budget cap rather than a human noticing.

**One bot account per agent becomes required rather than optional.** R3.6a's [later] already
sketches it; here it is load-bearing, because the classifier's policy keys on room membership and
membership means nothing if the agents share an MXID.

Recorded so the trade is explicit rather than forgotten: the archived dispatch plane's job/result
shape had none of these problems, because request/response has no shared store to sit at a floor.
What the room buys instead is that the operator can watch the coordination happen and can join in,
which the job shape never offered.

## The named workloads need less than the classifier

The three the operator named are well chosen: each is already low-trust-shaped, and **none of
them requires the classifier to be good**, because none hands the lower tier any of the higher
corpus.

- **k8s diagnostics.** `cluster-diagnostics-reader` is deliberately **secret-free** — no
  `secrets`, no `pods/log`, no `configmaps` — which is exactly what makes it delegable. The line
  to hold is that the other readers (`logs-configmaps-reader`, `secrets-reader`) are separate
  bindings and must not follow it down. "Nonsensitive k8s stuff" is not a property of Kubernetes;
  it is a property of that one ClusterRole, and the split already exists
  (<../../cluster/k8s/agents/agent-rbac-base/README.md>).
- **ducktape code.** Public, and trusted-by-**review** rather than by author — so a lower-tier
  agent proposing a PR is safe by construction, since the operator's review is the boundary. The
  prerequisite is plumbing, not trust: multi_agent.md's Forgejo mirror automation plus PR rights.
- **Kitchen.** <kitchen_stocking_subagent.md>, over the generalization multi_agent.md already
  states: **bounded-write MCP servers are how low-trust agents get safe write capabilities** — the
  credential stays server-side and the operations are bounded, so the caller's tier stops
  mattering.

The pattern under all three, and the thing to build toward first: **delegate a capability, not a
corpus.** Everything that works this way lands before the classifier exists, and is not waiting
on it.

## Open questions

- **Where does the tier label live, and who may set it?** With agent kinds keyed on their
  workspace repo, the repo is the natural home — which makes it a ducktape manifest fact and
  therefore not Haku's to change. The residue is rooms: a room's floor is derived from
  membership, so **whoever may invite may downgrade**, and that authority needs naming.
- **Does a lower-tier agent's memory inherit the tier of what it was told?** Anything durable it
  keeps — a state repo, a scratch note, a session that outlives the task — carries what it was
  told forward into unrelated work unless the tier travels with it. Its workspace repo bounds
  where it can write that down, which is one more reason the repo is the right label.
- **What happens on refusal?** A held message is visible as held, but the agent still needs to
  learn something — telling it exactly what was withheld and why hands an adversarial author the
  classifier's decision boundary, while telling it nothing makes it retry the same disclosure
  differently. Leaning toward: tell the agent it was withheld, tell the **operator** why.
- **How much does the operator want to see?** Coordination chatter between agents in a room the
  operator is in becomes a notification surface. Muting it defeats the point of being there;
  not muting it makes the room unusable for the operator's own conversation with Haku, which is
  an argument for inter-agent rooms being separate from the operator's DM even though the
  operator is a member of both.
- **The oai prompt line** — still open from multi_agent.md, and it is the values call this whole
  design is parameterized by.
