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

## Flows the design has to satisfy

Latency is one property of this component and not the only one it can get wrong. What follows
walks each thing a reader does, from the gesture to the requests to what the DOM ends up holding,
so a proposed design can be checked against all of them rather than against the open path alone.

**Read from the code, not run.** Cursor arithmetic below comes from `conversation_entity_interest`
(<../app/trajectory.py>) and `ConversationCollection` (<../app/frontend/conversation_store.tsx>);
the two marked **unverified** are predictions that need a browser test before anyone relies on
them. `_PAGE_SIZE` is 30 throughout, and a "segment" is an entity of kind `item`,
`confirmed_input` or `lifecycle` — a turn emits lifecycle rows too, so segments accrue faster than
messages do.

### Vocabulary

An **interest** is `(anchor_cursor, tail_from, window_from, window_before)`, resolved by the app
from the projection checkpoint. A **selection** is an interest plus the collections built from it.
`anchor_cursor` is the checkpoint's `through_cursor` **frozen at the moment the interest was
resolved**; `tail_from` is the 30th-largest segment cursor at or below it. The entity shape's
predicate is `cursor >= tail_from` with **no upper bound**, optionally unioned with a single
history page `window_from <= cursor < window_before`.

Two consequences follow from that and matter everywhere below: new segments land in the open shape
without any request, and a reader holds **at most one** history page, because the interest has one
`window_from`/`window_before` pair rather than a list.

### 1. Open at the tail

`GET /sync/interest` resolves the interest. The entity shape is created and snapshotted, and every
rendered body is fetched separately — the cost this plan opens with.

### 2. New items stream in while the reader is at the bottom

Nothing is requested. The segments are above `tail_from`, so they are already inside the open
shape's predicate and arrive on the live long poll, which returns and is re-issued. The virtualizer
appends; `followPreviousBottom` keeps the viewport pinned to the bottom as cards grow.

This is the case the current design handles well, and the one to avoid regressing.

### 3. Scroll up one page

At `scrollTop < 80` the view calls `onLoadOlder(boundary)`, where `boundary` is the cursor of the
**second** segment currently held — one segment of deliberate overlap, so the virtualizer has a row
it has already measured on both sides of the change.

That sets `beforeCursor`, which re-resolves the interest and builds a **new selection**. The old
collection is evicted. The reader's place is restored rather than preserved: `readingAnchor`
records the first visible row's cursor and pixel offset, and `correctRestoration` finds
`[data-conversation-anchor="<cursor>"]` in the new render and corrects `scrollTop` by the
difference.

Two properties of this are worth stating plainly, because the rest of the plan depends on them:
paging **replaces** the history window rather than extending it, so scrolling up twice drops the
first page; and place is kept by **measure-and-correct after the swap**, not by keeping the nodes.

### 4. Scroll up, then an item streams in — **unverified, believed broken**

This is where the arithmetic stops working. `ActiveConversation` rotates the whole selection once
it holds more than 60 segments:

```tsx
useEffect(() => {
  if (segmentCount > 60) onRotate();
}, [onRotate, segmentCount]);
```

Counting what a reader holds after one page up. The tail admits 30 (`cursor >= tail_from`, where
`tail_from` is `segments[0]`); the history page admits the 30 below `segments[1]`, which is
`segments[0]` plus 29 older — the deliberate overlap from flow 3. So **59 distinct**, and the
threshold needs 61. Two streamed segments reach it, and the selection rotates.

What comes back is the problem. `/sync/interest` re-resolves with `anchor_cursor` unset, so the
anchor becomes the current `through_cursor` and `tail_from` moves up by the two segments that
arrived — the tail is now `segments[2]` and newer. The history page has not moved: it still ends
strictly below `segments[1]`. **`segments[1]` is now in neither window.** A hole opens in the
middle of what the reader is looking at, and it widens by one on every rotation after that, since
the count returns to 60 and each further segment trips it again.

So the predicted behaviour is: rotate every streamed segment while a history page is open, and lose
one already-rendered segment from the middle of the view each time. Flow 3's place-keeping is
measure-and-correct against `[data-conversation-anchor]`, so the anchor row itself can be the one
that disappears.

W9 as drafted makes the cost worse without touching the cause: a rotation would rebuild two shapes
instead of one, and the content shape replays from `offset=-1`, so the window's whole text
re-transfers each time. Eager window content is the right trade for an open (§ W9) and the wrong
one for a per-segment rebuild.

**All of this is read off the code rather than run.** It needs a browser test that scrolls up and
then streams — asserting no hole and a bounded number of shape handles — before anything is
designed around it. If it holds, a fix comes **before** W9, not after.

### 5. Rotation on a long turn at the bottom

With no history page, the same threshold trips once about every 31 streamed segments. There is a
server-side counterpart: `/sync/entities` re-requested with a frozen `anchor_cursor` raises
`ConversationInterestExpiredError` once more than `_PAGE_SIZE * 2` segments sit above the anchor,
which the proxy turns into 410 and the client turns into a rotation. The two count different
things — the client counts every segment it holds, the server only those newer than the anchor —
which is why a history page moves the client's threshold and not the server's.

**Why rotation exists at all is an open question, and the first one to answer.** What it bounds is
the shape's width and the collection's size in memory: the predicate has no upper bound, so without
rotation a shape opened at the start of a long turn keeps widening. What it does **not** bound is
the DOM, which is virtualized independently.

Against that it costs exactly what § W5 says a moving bound costs — a new shape definition, a new
handle, no reuse between opens or readers — and it spends a scroll restoration each time. So the
mechanism this plan criticises as churn in W5 is also deliberately triggered on a timer here. Two
directions worth costing before either is built:

- **Keep the bound, drop the rotation.** If a wide shape is acceptable — and W5's page-aligned
  bound is what would make it cheap — then growth is a client-side eviction concern, not a reason
  to redefine the server's partition. A reader at the bottom does not need the segments it scrolled
  past to stay in its collection.
- **Keep the rotation, make it free.** If the bound is page-aligned, a rotation moves it by a whole
  page rather than by one segment, so consecutive rotations reuse a shape the server already holds
  and the swap stops meaning a re-transfer.

Either way the 60-segment threshold wants to be stated as a bound on something specific, rather
than as `page_size * 2` in two places that count differently.

### 6. The connection drops and comes back

A terminal stream error with `status === 0` is treated the same as an expired interest: rotate the
whole selection. That is the heaviest available response to a transient blip — a new interest
fetch, new shapes from `offset=-1`, and a scroll restoration — where the Electric protocol's own
`handle` + `offset` resumption exists precisely so a reconnect re-attaches to the shape it was
already reading.

What is unestablished is whether the collection can survive that error at all: the comment at the
call site says the adapter preserves a ready collection after terminal stream errors, and rotating
is how the code gets a live stream back. Whether TanStack's Electric collection retries internally,
and under what conditions it gives up, has not been checked against the pinned version. Until it
has, "reconnect resumes rather than rebuilds" is a requirement this design does not yet meet.

### 7. The projection scope is replaced underneath a reader

A rebuild publishes a new `projection_epoch`. Every shape is scoped to it, so the old ones 410. The
pending-selection mechanism is what this was built for: the new selection syncs in a `hidden`
subtree and is swapped in only once its `view_state` has caught up to the interest's
`through_cursor`, so the visible tree never blanks. Covered by
<../debug/conversation_acceptance.md>.

### What writing these out changes

- Flows 2 and 7 are load-bearing and work; a design that breaks either is not an improvement.
- Flow 4 is a defect in the current design that W9 would amplify, so W9 is **blocked** on deciding
  what rotation is for (flow 5) rather than the other way round.
- Flow 3's place-keeping is measure-and-correct, which is the mechanism most sensitive to a window
  being rebuilt underneath it. Every extra rotation is another chance for it to visibly fail.
- Flow 6 names a requirement — a reconnect resumes — that nothing currently verifies.

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
  `kind` on its entity row, so a shape on `field = 'text'` drags in every reasoning body — which may
  stay omitted until requested, and which `ContentSelection` already names as its own kind. The
  criterion for the window is **what the page always renders**, and reasoning fails it: it renders
  behind a `RetainedDisclosure`, exactly like tool arguments and output, and a reasoning trace is
  routinely longer than the answer it precedes. Splitting the field is what lets the window carry
  the one and not the other; the item `kind` cannot, because a shape predicate selects over the
  chunk table, which has no kind.
- **The extent on `PayloadRef`** — `content_bytes`, the value's whole length at the revision. A
  shape carrying many bodies hands a reader more than the revision its metadata names, so each body
  needs a bound it can apply without a second request. This is also a correctness requirement on its
  own; see below. A chunk count beside it would be redundant and worse than redundant: the byte
  length is a projection fact, while how many chunks a value occupies depends on how the store
  batched the writes, so the two can disagree.
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
