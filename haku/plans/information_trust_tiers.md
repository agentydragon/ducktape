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

## Three channels, three mechanisms

"Run a classifier on it" is one answer to three different questions, and they are not the same
question.

| Channel            | What crosses                     | Right mechanism                                     |
| ------------------ | -------------------------------- | --------------------------------------------------- |
| Delegation payload | the task the higher tier authors | local classifier, synchronous, fail closed          |
| Returned result    | the lower tier's output          | an **integrity** control, not a confidentiality one |
| Git push           | content leaving into a repo      | the destination, **not** the proxy — see below      |

The asymmetry is worth naming: outbound is a disclosure control, inbound is an injection control.
multi_agent.md already says sensor output is untrusted input to Haku; a worker's returned result
is the same thing, and needs no classifier — it needs the treatment every adversarial source
already gets.

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

## Matrix between agents: what it costs

Attractive because it is built, keeps history, and the operator can watch. Three costs, in
increasing order of how much they change the design.

- **A shared room sits at the trust level of its lowest member, permanently.** Every member's
  homeserver copy is readable by that member's agent, and a room is append-only and federated, so
  nothing said in it can be taken back. A room shared across tiers is a standing downgrade
  channel — and it would force the classifier to gate _every message_ rather than delegation
  payloads, which is exactly the shape argued against above.
- **Pairwise rooms are what survives**, and matrix_chat_runtime R3.6a's [later] already names the
  structure: one room per `(operator, agent)`, one bot account per agent. Extending the key to
  `(agent, agent)` keeps every room two-party, so each has one well-defined tier pair and one
  policy. The group room is the thing to refuse.
- **Everything R3.5 bought comes straight back.** matrix*chat_runtime puts mention gating,
  per-room sender allowlists and multi-bot loop protection out of scope \_because it is a DM*. Two
  bots in a room reinstates all three, and the loop is the dangerous one: two agents answering
  each other burn budget with no human in the loop. An inter-agent room needs a turn and budget
  cap that is not "the operator notices".

Worth weighing against what it replaces: the archived dispatch plane's job/result shape had none
of these problems, because it was request/response with no shared store. Matrix buys observability
and history and costs a shared mutable transcript at the lowest common tier. If the reason for
Matrix is that the operator can watch, note that the console now has a session view
(<../console/plans/session_channels.md>) — the same visibility without a shared room.

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

- **Where does the tier label live, and who may set it?** A per-corpus label is only as good as
  its authority. If Haku may set it, Haku may relabel; if only ducktape may, every new source
  costs a PR. This is the decision the rest of the design hangs off.
- **Does a lower-tier agent's memory inherit the tier of what it was told?** Anything durable it
  keeps — a state repo, a scratch note, a session that outlives the task — carries the delegation
  payload forward into unrelated work unless the tier travels with it.
- **What happens on refusal?** Silent redaction, a refused delegation, or an operator prompt.
  Only the last is honest when the classifier is the component most likely to be wrong.
- **The oai prompt line** — still open from multi_agent.md, and it is the values call this whole
  design is parameterized by.
