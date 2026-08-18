# Information trust tiers — which provider may see what, and who enforces it

**Status: design sketch, nothing built.** Operator, 2026-08-15: several agent kinds, each cloning
a different workspace repo, talking to each other in Matrix rooms the operator is also in, with
the high-trust agent delegating unsensitive work down.

**v0 ships the structural half only** — workspace repos, room tiers, enforced membership — and
governs message content by instructing agents not to share, with a classifier added later as a
backstop. What that bet does and does not rest on is § v0 below; the classifier's design is kept
here because deferring it is not the same as leaving it unshaped. Git-content classification is
out of scope and lower priority, with its findings parked for the plan it needs.

**This supersedes the zone experiment.** <../archive/2026_08_multi_agent.md> — the dispatch plane and its
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
  which is exactly what 2026_08_multi_agent.md already anticipates in wanting `ui/` out of `haku-state`
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

## Corpus separation covers past conversations, not just repos

Operator, 2026-08-15: an agent reads only the transcripts and conversations its tier gives it
access to. That **settles a deferral that has been open on purpose** — the Matrix channel leaves
reads unscoped and says so explicitly (<../console/x/channels/matrix/SPEC.md> § The agent's own
view), on the grounds that with one operator, one Haku and one room a fence would separate Haku
from its own history and nothing else. Several agents at several tiers is exactly the condition that premise was waiting on.

Note what the fence is and is not: **the tier, not the room.** Cross-room and cross-session reads
stay open _within_ a tier, which keeps an agent's own history reachable and only cuts the edges
that cross a boundary.

R5.3a also predicted where this lands, and that prediction holds: every read goes through the
console, which knows the calling Agent and each conversation's owner, so this is a decision
function at one call site in the shape the approval policy already has — not scoping smeared
through the transport. Three things it needs:

- **A conversation needs a tier of its own.** A session's room is its conversation's live
  `chat_attachment`; the tier goes beside it, derived from the room's fixed tier for a Matrix
  conversation and from the agent kind otherwise. Those two must agree, and the room's is
  authoritative where both exist.
- **Unlabelled is highest, so it fails closed.** Every session predating the column has no tier
  and must read as top-tier — unreadable by anything lower — rather than as "unclassified,
  therefore fine".
- **Semantic search is where this bites first, and it is already live.** `haku_index`'s `search`
  and `index_status` are exposed to Haku **unscoped**, decided 2026-08-15 and auto-approved through
  `haku_recall_reads` — an easy call at the time because it granted no new reachability over
  `haku_conversations`, which was itself unscoped. <../recall_index/README.md> § Read scoping names
  the exact condition for revisiting: "the moment a second operator or a room Haku should not see
  exists, ranked retrieval is where that leaks first". Several agents at several tiers is that
  moment. A drilldown makes reading another conversation deliberate — you have to name the
  session; ranked retrieval surfaces it by accident, at the top of the results, in answer to an
  innocent question.

### Corpus as a type, with an instance per tier

Operator, 2026-08-15, and it is a better mechanism than the per-row tier predicate this document
proposed first. `Corpus` in <../recall_index/schema.py> is today an enum of `git` and `chat`, doing
two jobs at once. Split them: **the type says how content is indexed, the instance says who may
read it.** One `chat` instance per tier, one `git` instance per repo.

The type keeps the job it already has, which is real — `chunker_key` is scoped by corpus because
the two chunk by different rules, and `content_sha` means a blob sha for `git` and a hash of a
rendered message window for `chat`. The instance is the new axis, and **the argument for putting
it in the primary key is the one the enum's own docstring already makes**: different namespaces
that a lookup forgetting to name is a bug the key shape should catch.

**Why this beats filtering rows by tier at query time:** enforcement moves to index time. The
sweep routes a session's chunks into a corpus; the gate becomes "which corpora may this agent
search", which is coarse, structural, and the kind of check doctrine prefers. A missed predicate
on one read path leaks — a corpus an agent cannot name does not. The tier does not disappear; it
becomes the sweep's **routing input** rather than a filter every reader must remember.

**A shared embedding across instances is fine, and the schema already says why.** An earlier draft
of this section claimed content addressing made cross-instance sharing a leak — that a `chat` row
serving a high and a low corpus at once was the very thing being prevented. It is not, and
`ChatChunk`'s own docstring makes the argument: it is keyed by position in the session rather than
by content, "because two sessions can hold the same exchange verbatim: they are then two windows
sharing one cached vector, and a search that matches it must be able to say which session each hit
came from."

That is the separation this design needs, already built:

- **`chunks` is a content-addressed embedding cache.** Sharing a row is sharing an embedding of
  byte-identical text. A searcher who can reach it through their own corpus was entitled to that
  text anyway, so nothing crosses.
- **The occurrence rows carry identity** — `chat_chunks`/`chat_chunk_messages` say which session
  and which messages, `git_tip` says which path at which commit — and those are what a hit hands
  back, since the index returns pointers rather than content.

**So the instance goes on the occurrences, not in the chunk key.** `chunks` keeps `corpus` as the
**type** (a blob sha and a rendered-window hash really are different namespaces, and `chunker_key`
is scoped by it) and gains nothing. The dedup win holds for chat exactly as it does for git.

What it touches, none of it large:

- **Occurrence tables become per-instance, and one wart disappears.** The two git tables are not
  duplicates of each other, which is worth stating since the names suggest it: `git_tip` is one
  row **per path** at the indexed commit — the tip's contents — while `git_sync_state` is a single
  row saying **which** commit that is, under which chunker and model, plus what the remote head
  was last seen pointing at. They cannot merge: the remote half is true before anything is indexed
  at all, so it would need a row with no path, and the indexed commit would otherwise repeat on
  every path row. Per instance, `git_tip`'s key becomes `(index, path)` and `git_sync_state` gets
  one row per index — which **removes** its `id = 1` singleton CHECK, because the index name is a
  natural key where `id` was a placeholder for having none. Chat occurrences already carry
  `session_id`, so their instance can be derived from the session rather than stored; derive
  first, store only if the join proves hot.
- **Filter occurrences before ranking, not after.** The searches already materialize a CTE
  filtering `corpus` + `model_key` before the distance operator runs — load-bearing rather than
  cosmetic, since pgvector errors on mixed dimensions — so the permitted-instance join belongs in
  that same CTE. Ranking globally and dropping afterwards would work, but a short top-K would then
  hint that hidden matches exist.
- **The sweeps run per instance**, with an advisory lock each; one lock per type iterating its
  instances is the simpler first step, exactly as with the Matrix supervisor.
- **`recall_index_reader.py` is where the grant lands**, and mostly **shrinks**: it exists to map
  the tool surface's `haku_state`/`conversations` vocabulary onto the index's `git`/`chat`, and
  with named indexes the names are the config, so the mapping stops being a hardcoded translation
  and becomes a permission check.
- **The grant is config in a shape that exists.** `haku_recall_reads` is one atom granting the
  index reads; it becomes per-index grants.

### Configuration: named indexes with type-discriminated settings

Operator, 2026-08-15: **"index `foobar` is of type `git`, indexes this remote"**, with room for
more settings later. That is a named instance carrying type-specific configuration, and this
codebase already has the pattern — `mcp.servers` entries select a discriminated `backend`
(`remote_mcp` with a URL and auth, or `in_process` with a credential kind), and STYLE prefers a
union of variant types over a flag plus optional fields that permit nonsense:

```yaml
indexes:
  - name: haku-state
    type: git
    remote: ...
  - name: ducktape
    type: git
    remote: ...
  - name: conversations-high
    type: chat
    tier: high
```

A `git` index names a remote; a `chat` index names which sessions it covers, which is where the
tier does its routing work. `index_status` becomes per index, and the tool's `corpus` argument
becomes an index name — so adding a second repo stops being a schema question and becomes a config
entry.

**The migration is cheap because embeddings are a cache.** `chunks` is derived data, recomputable
from its sources, so a mis-routed occurrence is a re-index rather than a loss. That makes this much
safer to do early than late.

**It makes the RLS option more attractive**, without deciding it. <../recall_index/README.md> keeps
a scoped-Postgres-role alternative in its inventory; with corpora as first-class the natural RLS
policy is per corpus, which would move the gate from the query builder into the database.

## Running more than one agent at once

**The session machinery is already there.** Concurrent sessions work today: the SPA surface and the
Matrix surface run as ordinary separate sessions, separate rows, separate sandboxes. Nothing about
several agents needs the turn loop, the store or the bridge to change.

What enforces "one Matrix session, one room" is three specific things, and only the first is a
migration:

1. **`matrix_conversation`'s primary key is the bot's MXID**, so a second room cannot be recorded
   without displacing the first — deliberately, as the schema's own docstring says, to make R3.6a a
   property of the schema rather than a rule code must remember. Widening the key to
   `(user_id, room_id)` is the change R3.6a's [later] note already anticipates.
2. **The supervisor and ingress load that single row** by configured bot user and assume one. They
   become "iterate the bindings" and "resolve the binding from the inbound message's room".
3. **Both advisory locks are single global constants.** One leader supervising every binding is
   enough for two, and is far simpler than a lock per binding — take that only if a stalled
   provision must not delay another room.

**The cheap increment is two rooms on one bot account**, which is what the operator asked for. The
sync loop needs no change at all: `/sync` on one account already returns events for every joined
room, so one loop and one `MXSY` lock serve N rooms. `matrix_sync_watermark` is keyed by `user_id`
already, so even the two-bot-accounts version — which distinct agent kinds eventually need, since
the room-tier policy keys on membership and membership needs distinct MXIDs — costs a second
credential and a per-account sync lock rather than a schema change.

**Budget for the sandboxes before doing this.** Sessions are always-up, so N rooms hold N sandboxes
continuously — at ~1 CPU / 2Gi against an 8 CPU / 16Gi quota, two idle rooms is a quarter of it
doing nothing. <../console/plans/conversation_layers.md> § 9's conversation-owned prompt queue —
a sandbox because there is something to do — is what makes this scale, and it is worth landing
with multi-session rather than after it.

### Talking to a particular agent: one Matrix account each

Operator, 2026-08-15, and it is the right answer for a reason beyond addressing: **the room-tier
policy keys on membership, and membership means nothing if the agents share an MXID.** So distinct
accounts are load-bearing rather than cosmetic, and they also make "open a conversation with agent
X" need no new interface at all — the operator starts a DM with `@haku-x`, and their client
already shows one conversation per agent.

What it costs, given what the singleton audit above found:

- **Nothing schema-side for the watermark.** `matrix_sync_watermark` is already keyed by `user_id`, so
  N accounts are N rows today.
- **A sync loop per account, and `MXSY` stops being a constant.** The lock has to derive from the
  account, or one loop has to serve several accounts; the former is closer to what exists.
- **Config becomes a list.** `MatrixConfig` carries one `user_id`/`password` pair.
- **Each account is provisioned, never hand-minted.** `cluster/provisioners/matrix_user_provisioner`
  already registers `@haku` from a SOPS password (R10.3); each agent kind gets the same treatment,
  under the same doctrine that governs Haku's Forgejo tokens — GitOps produces the credential, and
  live drift is fixed by fixing the provisioner.
- **R3.6's join rule generalizes unchanged**: each account joins only invites from the operator's
  own MXID.

**The tier of a new DM should be derived, not asked for.** A DM with agent X is a two-party room
whose only members are the operator and X, so it can be created at **X's own tier** with no
operator gesture — which keeps the common case free. Only a multi-party room needs its tier
declared, and that is exactly where declaring one is worth the friction.

One console consequence, small but worth catching early: the sessions surface
(<../console/plans/conversation_layers.md> § 9 step 2) shows each session's surface and room,
and with
several agent kinds it needs to show **which agent** too, or two rooms' sessions become
indistinguishable in the list.

## Attachment and subscription: the room model a shared room forces

Operator, 2026-08-15: **an agent may have its transcript connected to a Matrix room; it may also
subscribe to other rooms and use a separate tool to send into them.** That is the right
decomposition, and it is not an addition to the current model so much as a split of something
that is one thing today only because there has only ever been one room.

**Why the split is forced rather than chosen.** Auto-forward (R11.1) is 1:1 by construction — "what
the agent says at the end of a turn is what the room sees" has no meaning once there are two
candidate rooms. So exactly one room can be served implicitly, and every other room needs an
addressed send. The two relations are genuinely different:

| Relation       | How many    | Inbound                      | Outbound                      |
| -------------- | ----------- | ---------------------------- | ----------------------------- |
| **Attached**   | at most one | wakes a turn, as today       | auto-forward, no tool         |
| **Subscribed** | many        | context; waking is **gated** | explicit tool, room-addressed |

Six consequences, in the order they will bite:

- **Subscriptions belong to the agent, not the session.** Sessions rotate on compaction and
  failure, and a subscription is a durable property of an agent kind ("the ducktape agent watches
  the coordination room"). Hanging them off `session_id` loses every subscription at each
  rotation — the same data-losing shape R11.3a already flags for room bindings. The attachment
  behaves as `matrix_conversation` does now: owned by the agent, with a session pointer that moves.
- **`chat_attachment` is the right table, with a role.** Migration `0064` creates it as
  `(attachment_id, conversation_id, surface, address, attached_at, detached_at)` with a partial
  unique index on the address. What this section asks of that design: add `role`, and a
  second partial unique index enforcing **at most one attached room per agent**. One table, and
  the invalid state — two transcript homes — is unrepresentable rather than checked.
- **Wake on everything, and cap rather than gate** (operator, 2026-08-15). An earlier draft made
  mention gating a requirement here, on the grounds that waking for every message in a busy room
  is how several agents burn budget answering each other. **That conflated two jobs.** Mention
  gating reduces noise; it does not prevent the loop, because two agents addressing each other by
  name loop just as happily. What prevents the loop is a **per-room turn and budget cap**, and
  that is needed whether or not messages are gated. So v0 wakes on every message in a subscribed
  room — the same rule as the attached room, and one fewer concept — with the cap doing the
  safety work.

  Two existing mechanisms make this far more viable than it sounds, and both are already built:
  the debounce (R2.7) coalesces a burst into one turn, and serialized turns (R2.2) hold messages
  arriving mid-turn for the next prompt. A busy room therefore produces far fewer turns than
  messages before any gating is considered. Mention gating stays available as a noise
  optimization if the volume proves annoying, which is a much better reason to add it than
  safety was.

  R1.5's "never treat your own events as input" still generalizes: an agent must ignore its own
  sends into subscribed rooms, or it answers itself in every room it can write to.

- **Provenance grows a room.** R2.4 gives each batched message a sender, timestamp, event id and
  thread root. With several rooms in one context the agent cannot address a reply without knowing
  which room each message came from, and which room is its own — so the room joins the batch's
  provenance and the prompt has to say which one is home.
- **A new session must be told what it is subscribed to** (operator, 2026-08-15), and this is a
  correctness requirement rather than a courtesy. Subscriptions outlive sessions by design — that
  is the point of hanging them off the agent — so a replacement session inherits a set it has no
  way to discover. Without being told it receives messages stamped with room ids it has never
  heard of, which makes the provenance above uninterpretable and any addressed reply a guess. It
  is also the precondition for the attention tool below: an agent cannot sensibly mute what it
  does not know it is watching.

  This belongs with the facts the system prompt already carries — identity, the room, the session
  id (R7.3), the harness contract, the recent messages — and it is the same class of fact as those:
  something the agent must be handed because it cannot derive it. R3.3a's re-awakening is where it
  lands, since that is the path a rotated session takes.

- **The send tool takes a room id, which R5.3 forbids — deliberately relaxed, not overlooked.**
  R5.3's property was that reaching another room is "not expressible rather than merely denied".
  That cannot survive a tool whose whole job is addressing another room. What replaces it is
  nearly as strong and is the reason this stays safe: **the console validates the room against
  that agent's subscription set**, which is server-side, small, and itself constrained by the room
  tier policy. So an agent can name only rooms it was put in, and it was put only in rooms its
  tier allows. Reaching up is refused by the same rule that governs membership.
- **Both sends still go through the outbox.** Auto-forward for the attached room and the tool for
  a subscribed one converge on the same queue, so the classifier gates them identically and there
  is no second delivery path to secure separately.

### Letting the agent manage its own subscriptions

Operator, 2026-08-15, as the alternative to gating: give the agent a tool and let it say what it
wants to watch. That is a good idea **provided one distinction holds**, because "subscription"
covers two things that must not share a tool:

|                | What it decides                                         | Who owns it                         |
| -------------- | ------------------------------------------------------- | ----------------------------------- |
| **Membership** | which rooms the agent is _in_ — what it can read at all | console and operator, tier-enforced |
| **Attention**  | which of its rooms wake it and feed its context         | safe to hand to the agent           |

**Attention is safe to delegate precisely because every option is already permitted.** An agent
choosing to stop watching a room it is a member of grants itself nothing — it is an
`unread`/`mute` decision over a set the tier policy already fixed. Membership is the opposite: a
tool that could cause a **join** would let an agent widen what it can read, which is the one thing
the room-tier design enforces and why R5.4 excludes join, invite and leave. Keep them apart and
the tool is nearly free; merge them and it is the whole boundary.

So the tool manages attention over the agent's existing membership, and asking for a room it is
not in is a request the operator answers, not an action the agent takes.

**One failure mode to design against, and R3.6a already names its mirror image.** That requirement
rules out silently joining a room nothing services, because "it looks like Haku is listening when
it is not". An agent muting itself is the same failure from the other side. So a self-mute should
be **visible in the room** as a notice, and probably bounded — expiring rather than permanent — so
the default drifts back to listening rather than to silence. The attached room is not mutable at
all; that one is the transcript home.

This also composes with waking on everything: the reason to skip mention gating is that the volume
is manageable, and if a particular room proves noisy the agent can say so itself rather than the
harness guessing a rule.

**What does not change**, and is worth saying because this looks like the thing the plan ruled
out: the console still holds every Matrix credential, the harness still owns ingress, and no agent
polls `/sync`. matrix_chat_runtime's non-goal is "give the agent a Matrix account and let it drive
the API", with the surviving rule "the harness owns ingress and the reply channel; agent tools are
write-side extras and targeted reads, never the delivery path". A console-side send tool for
subscribed rooms is precisely a write-side extra. The non-goals section already anticipates a send
tool arriving as an in-process MCP server on the console; the one assumption it made that this
breaks is that such a tool would need no room argument.

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

### The outgoing queue, which is what makes it async

Operator, 2026-08-15: outgoing messages go into a **queue**, the classifier consumes the queue and
decides, and the first failed message halts — for that agent.

**This dominates both options an earlier draft weighed**, and it is worth seeing why rather than
just taking it. That draft posed synchronous (prevents the message, costs latency on every message
forever) against after-the-fact (costs nothing, but the message has already federated and is
unrecoverable). A queue in front of the room is **neither**: it is asynchronous with respect to the
_agent_, which never waits and does not know the check exists, and still strictly before the
_room_, because nothing has been posted when the classifier sees it. The latency moves from the
turn to the delivery — the room sees a reply a beat later — which is the cheap place to put it in a
chat surface.

Four properties it needs, and one of them is not obvious:

- **Ordered drain, per room.** A held message must not be overtaken by the ones behind it, which
  is what makes "the first failed message halts" a coherent rule rather than a race.
- **Hold, do not discard, what was queued behind the failure.** Those messages are the evidence
  for working out what happened, and they cost nothing to keep.
- **Halt scoped to the agent** is the right default here, narrower than the "widest is safest"
  this document argued before. The disclosure is one agent volunteering the operator's data, so
  stopping that agent stops the source; other agents in the room continuing is a feature, not a
  gap. Widen only if a failure is ever found that is not attributable to one sender.
- **The queue is the room outbox, and it is not new.**
  `session_outbox` turned the Matrix pacer's deque into rows for delivery reliability, and
  <../console/plans/conversation_layers.md> § 5 wants the same rows for channel reconciliation. This makes a third consumer, and the one that changes the priority: an outbox is
  a prerequisite for the classifier rather than a tidy-up, because a classifier needs somewhere
  durable to **hold** a message while it decides.

**So v0 builds the queue and no classifier — and the queue is already built.** `session_outbox`
and `channels/matrix/outbox.py`'s `RoomOutboxDrain` landed as stage 5 (#4104), with nothing
consuming them but delivery, which is exactly the reliability improvement that stage wanted. The
classifier lands later as a stage in front of the drain. That is a clean seam, it means the v0/v1
boundary costs no rework, and this half of v0 costs nothing at all now.

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
adversarial source already gets. 2026_08_multi_agent.md made the same point about sensor output, and it
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
there. That is the same reconcile loop <../console/plans/conversation_layers.md> § 2 describes,
over membership instead of messages.

**Operator presence is detection, not prevention.** By the time the operator reads a leak, the
event has federated to every member's homeserver and its provider has already seen it. That is
the argument for the classifier being synchronous and fail-closed rather than an audit log — and
equally the argument for keeping the operator in the room anyway, because their reaction is the
only calibration signal the classifier will ever get.

- **A withheld message has to be visible as withheld.** Silently dropping one leaves the operator
  unable to tell whether the agent answered, and leaves the other agents waiting on a reply that
  is never coming. It wants its own `RoomEventKind` in the tag vocabulary
  (<../console/x/channels/matrix/client.py>) so the room shows that something was said and held.
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
  prerequisite is plumbing, not trust: 2026_08_multi_agent.md's Forgejo mirror automation plus PR rights.
- **Kitchen.** <kitchen_stocking_subagent.md>, over the generalization 2026_08_multi_agent.md already
  states: **bounded-write MCP servers are how low-trust agents get safe write capabilities** — the
  credential stays server-side and the operations are bounded, so the caller's tier stops
  mattering.

The pattern under all three, and the thing to build toward first: **delegate a capability, not a
corpus.** Everything that works this way lands before the classifier exists, and is not waiting
on it.

## Open questions

- **What exactly the instruction says.** In v0 this **is** the control, not a supporting detail,
  so the old "oai prompt line" question from 2026_08_multi_agent.md is promoted to the top: which
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
