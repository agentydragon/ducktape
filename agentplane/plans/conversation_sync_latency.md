# Conversation sync latency

Opening a Thread on staging takes tens of seconds before its text paints, and text that has already
arrived renders much later than the response carrying it. A HAR showed requests spending ~20 s
upstream while their bodies transferred in milliseconds, which reads as slow Electric shape
creation. It is not. This plan records what the cost actually is and what to do about it.

## Measured, 2026-09-22

Against the deployed `agentplane-testing`, on threads whose shapes had never been created, through
the app's own sync routes:

| Stage                                     | 7-row thread A | 7-row thread B |
| ----------------------------------------- | -------------- | -------------- |
| `/sync/interest`                          | 0.54 s         | 0.37 s         |
| `/sync/entities` (offset=now + snapshot)  | 0.89 s         | 0.81 s         |
| 3 bodies (`payload-interest` + `-chunks`) | 2.90 s         | 2.53 s         |
| **total**                                 | **4.33 s**     | **3.72 s**     |

A cold entity shape took 0.37–0.62 s; a warm one 0.36–0.53 s. **Cold and warm are
indistinguishable**, so what is being measured either way is a round trip, and shape creation is
not the cost.

The cost is **request count**. Roughly 0.4 s per round trip, and the open path issues two per body.
Seven rows with three bodies already costs 4.3 s; the 30-segment tail this component specifies is
about sixty requests, and ~0.4 s each is the reported twenty seconds — not one slow shape but sixty
ordinary ones.

Read the absolutes as a ratio: these come through an agent HTTPS proxy from outside the cluster, so
a browser's round trip is smaller. What survives that is the shape of it — the open path is
`O(bodies)` round trips where it must be `O(1)`.

## What the open path does today

1. `GET /sync/interest` — the projection checkpoint and the 30th-last segment cursor.
2. `GET /sync/entities` — an Electric shape over `cursor >= tail_from`.
3. `GET /sync/payload-interest` — per rendered body, its manifest's `chunk_count`/`content_bytes`.
4. `GET /sync/payload-chunks` — per rendered body, a second Electric shape.

Nothing overlaps: `ConversationCollection` mounts no body until the entity shape has caught up.

## W9 — one shape for the window's content

`conversation_payload_chunk` carries `owner_cursor`, so a single shape over
`owner_cursor >= tail_from AND field IN ('text','confirmed_input')` — the entity interest's own
bounds — covers every body the page renders. The open path becomes four requests whatever the
conversation holds, and reasoning, arguments and output keep their own selected shapes for when a
disclosure opens them.

It also retires a distinction the conversation model never asked for. `follow` is currently derived
from `live && completion === null`, so a completed item is treated as final; the model says existing
items can change anywhere in the history. On a window shape there is nothing to derive: a body
updates because its chunks did.

Three pieces, one epoch bump:

- **`PayloadField.REASONING`.** Reasoning writes to `text` today, distinguished only by the item's
  `kind` on its entity row, so a shape on `field = 'text'` would drag in every reasoning body — which
  may stay omitted until requested, and which `ContentSelection` already names as its own kind.
- **The extent on `PayloadRef`** — `chunk_count` and `content_bytes`. A shape carrying many bodies
  hands a reader more than the revision its metadata names, so each body needs a bound it can apply
  without a second request. This is also a correctness requirement on its own; see below.
- **The shape per generation, not per revision.** A `chunk_index < n` bound makes shape identity
  depend on the revision, so every append defines a new shape and a completing item pays a cold
  creation for bytes its own stream already delivered. Bound the shape to the generation and let the
  extent bound the read.

### The extent is a consistency requirement, not an optimisation

A revision is what makes a body and the row naming it one fact. Remove the reader's bound and an
item's text can run ahead of its own metadata: `agentplane/app/test_thread_browser.py`'s
`test_http_admission_ahead_of_replay_does_not_skip_earlier_events` holds `/sync/entities` while
leaving `/sync/payload-chunks` alone, and a reader with no bound renders chunks the held metadata
has not reached. Contiguity from index 0 gives the whole value at _some_ revision of the
generation, which is not the one being shown.

## W5 — a shape is a partition, not a viewport

`conversation_entity_interest` sets `tail_from` to the 30th-largest segment cursor, and that value
is the shape's bound. Append one segment and the 30th-largest becomes what was the 29th: a new
bound, a new shape definition, a new handle. An idle conversation reuses its shape across opens; a
growing one defines a fresh one on every open, and rotation defines another.

A shape is a server-side cache with a log, maintained from the replication stream, shared by every
reader whose interest matches it and meant to outlive any of them — which is why the deployment
caps how many exist and evicts by use. A predicate carrying a continuously moving bound gives up
all of it: no reuse between two readers of one conversation, no reuse between two opens by one
reader, and an eviction queue churning behind both. A shape _per conversation_ is ordinary; a shape
per _view of_ a conversation is what this builds.

The measurement says this is worth sub-second per open, so it is hygiene rather than latency. The
fix is a **page-aligned bound over a stored per-segment index**: segments are append-only, so a
monotone index assigned at projection time is stable, and a bound rounded to a page admits between
one and two pages by construction while being redefined once per page instead of once per segment.
Cursor quantization cannot do this — how many segments a cursor range admits depends on how densely
a turn packs them, so no granularity is both stable and bounded.

One shape per thread — the whole conversation's metadata, stable forever — was considered and
**rejected**: opening a conversation loads its tail, not its history. That is a product decision,
not an inference from the measurement, and it holds whatever a row weighs.

W9 carries the same bound, so whichever partitioning this gets is the one W9 needs.

## Smaller items

- **W6 — the debug observations page** ships 30 full raw entries it renders none of; the listing
  should carry identity and the entry should load on expansion.
- **W7 — shape-creation telemetry.** `ElectricProxy._forward` should log upstream duration and the
  returned `electric-handle`: a cold creation and a warm snapshot are indistinguishable in a HAR,
  and counting distinct handles over a run is what measures churn.
- **W8 — the proxy answers `private, no-store`**, discarding the caching the protocol is built on
  and making the entity tag it already relays inert. `public` cannot stand on a caller-scoped
  response and neither can a `max-age`, since a browser's HTTP cache outlives a logout with no way
  to clear it. `no-cache` keeps the body and forces a request this proxy authorizes, and forwarding
  `if-none-match` lets Electric answer it 304.
- **W1 — Electric's shape storage** is `fast_file` on a `seaweedfs-ovh` PVC: distributed HDD over
  FUSE. The measurement says this is not where the latency is at current conversation sizes, and it
  is untested for a long conversation's first shape. A node-local volume would also pin Electric to
  one replica; a backing store several instances can share would answer this and horizontal
  replication together, and which of its backends allow that is unchecked.

## Measurement gates

- Open-to-first-text for a 30-segment tail, cold and warm, with upstream shape-creation duration
  reported separately from transfer. `agentplane/acceptance/test_conversation_latency.py` is the
  instrument, and it only exercises what the deployment is running.
- Distinct Electric shape handles created during one turn: zero new shapes per completing item.
- Small-file create/fsync latency on `seaweedfs-ovh` versus `local-path-ovh-ssd` from the Electric
  pod's node, before spending W1's PVC change.

## Out of scope

- Replacing Electric. The protocol usage is valid; the granularity is wrong.
- A longer-lived `Cache-Control` on payload bodies, for the logout reason above.
- The open acceptance gates in `agentplane/debug/conversation_acceptance.md`. Latency work does not
  close them and they do not block it.
