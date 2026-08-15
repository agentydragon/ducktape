# Information trust tiers — which provider may see what, and who enforces it

**Status: design sketch, nothing built.** Operator, 2026-08-15: several agent kinds, each cloning
a different workspace repo, talking to each other in Matrix rooms the operator is also in, with
the high-trust agent delegating unsensitive work down.

**v0 ships the structural half only** — workspace repos, room tiers, enforced membership — and
governs message content by instructing agents not to share, with a classifier added later as a
backstop. What that bet does and does not rest on is § v0 below; the classifier's design is kept
here because deferring it is not the same as leaving it unshaped. Git-content classification is
out of scope and lower priority, with its findings parked for the plan it needs.

**This supersedes the zone experiment.** <multi_agent.md> — the dispatch plane and its
zai/oai/local zones — is retired (operator, 2026-08-15), so it is cited below only for the
observations that outlived it, never as pending work. It also carried a dangling forward
reference to a `capability_dispatch.md` that was never written; that reference's subject is this
document's.

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

## v0 is structure only, and the classifier is a later backstop

Operator, 2026-08-15: agents will mostly be well behaved and will follow an instruction not to
share sensitive information, so **v0 ships without any classifier** and the classifier arrives
later as a backstop. That is a smaller bet than it sounds, and it is worth being precise about
what it does and does not rest on.

**What v0 still enforces structurally**, with no reliance on any agent behaving:

| Control                           | Enforced by                          |
| --------------------------------- | ------------------------------------ |
| Which corpus an agent starts from | its workspace repo, a manifest fact  |
| Which credentials it holds        | what is reflected into its namespace |
| Which hosts it can reach          | its egress perimeter                 |
| Which model provider it can reach | its LiteLLM route                    |
| **Which rooms it can be in**      | the room's tier, console-enforced    |

**What rests on instructions, and it is exactly one thing:** the content of a message the
high-trust agent chooses to type into a lower-tier room. Everything a lower-tier agent can
_reach_ is structural; the only path from the high corpus to a low room runs through Haku
deciding to write it down. That is a narrow trusted surface for a v0, which is what makes this a
reasonable place to start rather than an optimistic one.

**The deviation, recorded rather than glossed.** <../docs/security.md>'s doctrine is enforcement
at the perimeter, never by in-agent rules, and its threat model assumes Haku can be steered by
prompt injection through any source it reads. v0 governs message content by instruction alone,
which is an in-agent rule. That is a deliberate, scoped exception for one channel — not a
revision of the doctrine — and the condition for closing it is the classifier below. Anyone
reading the invariants and finding this inconsistent is reading correctly.

**A structured task envelope helps more here than it did before.** It was proposed as a way to
make a classifier's job tractable; with instructions as the primary control it does something
better, which is make compliance easy. "State goal, inputs and acceptance criteria in fields" is
a far more followable rule than "be careful what you mention", and it keeps free narrative — where
inadvertent detail actually lives — out of mixed rooms by construction.

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

## The classifier, when it comes

### What it is for

The threat is **not primarily a malicious lower tier extracting secrets**. It is the **high-trust
agent volunteering something that should never have reached a room a lower-tier agent is sitting
in** — Haku mentioning the operator's embarrassing medical condition while coordinating work, in
a room where an OpenAI-backed agent is a member and therefore its provider sees every word. It is
inadvertence rather than attack: a helpful agent supplying context. That is also why v0 can defer
it — an instruction is a reasonable control against inadvertence, and a poor one against an
adversary, which is the residual the backstop closes.

### Halt and catch fire

Operator, 2026-08-15: a triggered classifier may simply **halt** — no redaction, no
continue-without-that-sentence, no negotiating with the agent about what it may rephrase. That
choice deletes most of the design:

- **No withheld-message rendering, and no incoherent-room problem.** An earlier draft wanted a
  `withheld` event kind so the room stayed readable and the other agents were not left waiting.
  If a trip ends the conversation, incoherence is the correct outcome.
- **No "what does the agent learn" dilemma.** Telling an agent precisely why a message was
  refused hands an adversarial author the decision boundary; telling it nothing invites a
  rephrased retry. Halting answers both: nothing continues, so there is nothing to iterate
  against.
- **Recall matters more than precision.** A false positive costs an interrupted session and an
  operator's attention — annoying, safe, and visible. A false negative is unrecoverable, because
  a federated event cannot be unsent and the provider has already received it. Tune accordingly;
  this is the rare case where a jumpy detector is the right failure direction.

Two things still to decide, and they are cheap to decide late:

- **What "halt" covers** — the offending agent's session, the room's delivery, or every agent.
  Widest is safest and this is a backstop, so the burden is on arguing for narrower.
- **Synchronous or after the fact.** Synchronous is the only version that prevents the specific
  message, and costs latency on every message forever. Asynchronous costs nothing per message and
  still stops messages 2..N — which is less of a concession than it first appears, since the
  first message is already unrecoverable the instant it federates. Synchronous is the better
  default for a backstop whose whole job is the one message; asynchronous is defensible if the
  latency proves to matter.

### Where it runs: local is a preference, not a requirement

An earlier draft of this document argued that the classifier had to run on local GPUs or it would
disclose the content to the party it was deciding about. **That is wrong for the topology
actually wanted** (operator, 2026-08-15: Anthropic would be fine), and the reason is worth
keeping because it is what makes any future placement decision easy.

The tiers are not peers with symmetric ignorance. The **high tier is Anthropic** — Haku runs
there with `haku-state` mounted — so the sensitive content has already reached Anthropic by
construction, before any classification happens. A classifier hosted there discloses nothing new;
it is the top of the order, exactly as the archived plane assumed. The peers-with-different-
allowlists framing was about OpenAI's access being whitelist-shaped, and that is a statement
about what OpenAI may **receive**, not about a corpus Anthropic lacks.

So placement is an ordinary engineering choice, and both options are live:

- **Anthropic** — strongest model on the hardest judgment (paraphrase, inference chains, oblique
  reference), no GPU contention, no extra moving part.
- **Ollama on the operator's GPUs** — cheaper per call, lower latency, and no dependency on a
  provider being reachable to decide whether another provider may be spoken to. Its cost is real:
  a small model is the weakest link guarding the strongest secrets, and GPU model residency is a
  known constraint — <../x/dispatch/local_dispatch_zone.md> reached for a scheduler because
  running agents across several models thrashes it. A synchronous classifier in the send path is
  one more resident model, and one that must answer **now** rather than when a slot frees.

**The one condition that would make local structural**, worth writing down before it is
forgotten: a tier whose corpus Anthropic must not see. Nothing in the current design has one, and
if one is ever added the classifier's placement has to be revisited before that tier is.

### Where it hooks in, and what it does not cover

**The choke point already exists**, which is what will make the backstop enforceable when it
lands. The console holds the only Matrix credential and posts everything the agent says (R11.1
auto-forward — the agent has no send tool), so the classifier is a filter in the reply path,
before the room send, in reviewed console code the agent cannot reach around.

It covers **outbound** only. A lower tier's messages and results coming back are an **integrity**
concern, not a confidentiality one, and need no classifier — they need the treatment every
adversarial source already gets. multi_agent.md made the same point about sensor output, and it
survives that document's retirement.

### Git content: its own plan, and lower priority

**Out of scope here** (operator, 2026-08-15) — it needs a plan of its own and is not urgent.
Three findings to carry into that plan rather than re-derive:

- **The egress proxy is the wrong layer for content.** `haku-egress-proxy` rewrites a header for
  a known host, which is cheap because it inspects request _metadata_. Classifying pushed content
  means parsing git's smart-HTTP protocol and unpacking packfiles inside the proxy, on a path
  that has to stay fast, for payloads that are large, compressed and delta-encoded.
- **The destination already has the content unpacked.** A **Forgejo pre-receive hook** is
  synchronous and refuses the push, which is the semantics actually wanted; a **required CI
  status** on PRs is weaker (the content has already landed on a branch) but needs no protocol
  parsing at all.
- **The proxy is right for the other half.** Which repos an agent may push to at all is metadata,
  and the proxy already sees the request path.

That last one is where this joins something already wanted: <../TODO.md>'s runtime-steerable
egress grant — widening or revoking what the proxy permits mid-session, "the same shape as the
approval queue, applied to egress rather than to tool calls". A console-driven proxy is the
natural home for per-repo push scoping, and the two should be planned together.

## Shared rooms, with the operator in them

The chosen shape (operator, 2026-08-15): agents talk to each other in Matrix rooms the operator
is also a member of, and messages pass the classifier on the way in. This section is what that
requires — the objections that mattered are absorbed as requirements rather than as reasons not
to.

**The room carries a tier, fixed at creation, and membership is enforced against it** (operator,
2026-08-15). The room's tier is the maximum sensitivity that may be said in it; an agent may join
only if its provider is cleared for that tier. A `low` room can hold Anthropic- and
OpenAI-backed agents together; a `high` room admits only agents cleared for `high`.

**This is strictly better than deriving a floor from current membership**, which is what this
document proposed first, and it is worth being explicit about why so it does not drift back:

- **The classifier checks against a constant.** A message is judged against the room's declared
  tier, not against whoever happens to be present, so the same message gets the same answer
  regardless of who joined since. Under the derived-floor version, membership changes
  re-parameterized every future message in the room.
- **Structure and content separate cleanly.** Membership enforcement is a structural control
  deciding **who may hear**; the classifier is the in-band residual deciding **what may be said**.
  Two layers, each doing one job, and only the second is probabilistic.
- **The downgrade problem disappears.** A room could previously be spent permanently by adding one
  cheap agent to it. An immutable tier makes that unexpressible: the invite is simply refused.
- **So does the history trap, which was the sharpest requirement here.** Under a derived floor,
  `m.room.history_visibility` had to be `joined` at creation, because Matrix's default `shared`
  lets a new member backfill everything said before it arrived — unclassified against its
  presence. With an immutable tier, **anyone permitted to join is already cleared for the room's
  entire history by construction**, so backfill discloses nothing new. Still worth setting
  `joined` as defense in depth; it stops being load-bearing.

**What enforces membership.** Not the homeserver: Matrix membership is whoever gets invited, and
the operator can invite from Element by hand. The console is the enforcement point, on the same
property R5.1 already establishes for one agent and this generalizes — **the console holds every
agent's Matrix credential**, no agent has a join tool (R5.4), so no agent can put itself in a
room. That leaves the operator's own mistaken invite, which wants **membership reconciliation**:
the console compares each room's members against its tier and removes one that should not be
there. That is the same reconcile loop <../console/plans/session_channels.md> § 1 describes, over
membership instead of messages.

**Operator presence is detection, not prevention.** By the time the operator reads a leak, the
event has federated to every member's homeserver and its provider has already seen it. That is
the argument for the classifier being synchronous and fail-closed rather than an audit log — and
equally the argument for keeping the operator in the room anyway, because their reaction is the
only calibration signal the classifier will ever get.

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

- **What exactly the instruction says.** In v0 this **is** the control, not a supporting detail,
  so the old "oai prompt line" question from multi_agent.md is promoted to the top: which
  categories never enter a lower-tier room, stated concretely enough that an agent can follow it
  and the operator can tell when it was not followed. Seed conservatively and widen from
  observation.
- **Who declares a room's tier?** The tier is immutable once set, which makes creation the only
  moment that matters and the one authority to name. Agent kinds get theirs from a ducktape
  manifest and so are not Haku's to change; rooms need the same answer.
- **Does a lower-tier agent's memory inherit the tier of what it was told?** Anything durable it
  keeps — a state repo, a scratch note, a session outliving the task — carries what it was told
  forward into unrelated work unless the tier travels with it. Its workspace repo bounds where it
  can write that down, which is one more reason the repo is the right label.
- **How much does the operator want to see?** Coordination chatter in a room the operator is in
  becomes a notification surface. Muting it defeats the point of being there; not muting it makes
  the room unusable for the operator's own conversation with Haku — an argument for inter-agent
  rooms being separate from the operator's DM even though the operator is in both. It matters
  more in v0 than later, because until the classifier exists **the operator is the only
  detector**: nothing else will ever notice a leak.
