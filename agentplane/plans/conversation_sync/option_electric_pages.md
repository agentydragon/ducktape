# Option: TanStack DB + ElectricSQL, partitioned by page

**One option among several** — see <README.md> for the others and the fit matrix, and
<requirements.md> for what any of them has to do. This file is the TanStack + Electric branch worked
out to the point where its costs are visible, kept whole so it can be compared rather than
half-remembered.

It is the deployed stack, so it starts ahead on familiarity and behind on nothing except the two
It **satisfies D1**, which is worth stating up front because an earlier draft of this file claimed
the opposite. Pages never overlap and their bounds never move, so scrolling up subscribes to a page
the client does not hold and re-sends nothing. What it pays for that is subscription count — seven
shapes at the tail — which D1, as finally stated, does not charge for.

What it structurally cannot do is **P8/D2**: the content selection lives in a server-side shape
predicate, where a client cannot express it and the server cannot vary it per reader. P8 is in
<../../docs/thread_view_sync.md>, so that is this option's one hard problem, and it is called out
where it arises below.

**Note on numbering:** `W1`…`W9` in this file are _work items_ from the draft this grew out of, and
have nothing to do with the `D` wants in <requirements.md>. Requirement citations here are `P`, `S`,
`E` and `O`.

The measurement that opens this file is the grounding fact for **every** option, not just this one.

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

## What Electric gives us

Every flow below has to be built from these, so they are worth stating before the flows rather
than assumed inside them. Deployed: `electricsql/electric:1.8.1`, `ELECTRIC_STORAGE=fast_file`,
`ELECTRIC_MAX_SHAPES=1024` (<../../../cluster/k8s/agentplane-testing/agentplane.k8s.yaml>).

- **A shape is its predicate.** Identity is `(table, columns, where, bound params, replica)`. Two
  readers whose predicates match share one server-side shape, its log and its cache; change any
  bound and it is a **different shape** with its own handle, log and snapshot cost. This is the
  single fact the rest of the design turns on.
- **A shape has a log, addressed by offset.** `offset=-1` replays it whole. `offset=<O>&handle=<H>`
  resumes from a position, which is also how a reader that went away comes back without
  re-transferring. `live=true` long-polls for what comes after.
- **Two read modes.** `log=full` replays every change ever made — correct and cheap for an
  append-only table, since each row appears once. `log=changes_only` plus a **subset snapshot**
  (`offset=now`, then `offset=<O>&handle=<H>&subset__where=…`) bootstraps from current state
  instead, which is what a mutable table needs: replaying a full log would cost a reader one
  message per past revision of every row.
- **409 `must-refetch`** retires a handle whose log the server can no longer serve from. It is
  Electric's own signal and means rebuild this shape, not rebuild the reader's view.
- **Shapes are evicted by an LRU** bounded at 1024. A predicate that no two readers ever share, or
  that one reader never reuses between opens, spends this budget and gets no cache in return.

What Electric does **not** offer, and so cannot be designed around: no way to widen or narrow a
live shape in place (a new predicate is a new shape), and no server-side notion of a reader's
viewport. Anything viewport-shaped has to be assembled from shapes, client-side.

## Flows the design has to satisfy

Latency is one property of this component and not the only one it can get wrong. What follows
walks each thing a reader does, from the gesture to the requests to what the DOM ends up holding,
so a proposed design can be checked against all of them rather than against the open path alone.

**Read from the code, not run.** Cursor arithmetic below comes from `conversation_entity_interest`
(<../../app/trajectory.py>) and `ConversationCollection` (<../../app/frontend/conversation_store.tsx>);
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
<../../debug/conversation_acceptance.md>.

### What writing these out changes

- Flows 2 and 7 are load-bearing and work; a design that breaks either is not an improvement.
- **Every failure is the same failure.** Rotation moves `tail_from`; paging up replaces the window;
  a dropped stream rebuilds the selection. Each redefines a shape a reader is looking at, which is a
  new server-side shape and a set of rows that go away and come back. Flows 3, 4, 5 and 6 are four
  faces of that, not four problems.
- So the ordering the earlier draft had was backwards. It made W9 blocked on "what is rotation for",
  as though rotation were a policy to be tuned. Rotation is a **consequence** of a bound that moves,
  and the question to settle first is the partition — which is what the next section does.
- Flow 6's requirement is not unverified after all, as this plan first claimed:
  `test_projected_browser_streams_runner_events_and_loads_bodies_lazily` takes the browser offline
  and asserts the visible text survives. What was unverified was whether the design meets it.
- Flow 3's place-keeping is measure-and-correct against `[data-conversation-anchor]`, which is only
  needed because the window is rebuilt underneath the reader. Under a partition that is never
  rebuilt there is nothing to restore, and that machinery can go.

## A partition that satisfies every flow

The flows above fail in one place — every one of them, including the two that work, turns on
**redefining a shape while a reader is looking at it**. Rotation moves `tail_from`; paging up
replaces the window; a dropped stream rebuilds the selection. Since a shape _is_ its predicate
(§ What Electric gives us), each of those is a new server-side shape, a new snapshot and a reader
whose rows go away and come back.

So the fix is not to make redefinition cheaper. It is to stop redefining: make a reader's view an
**additive set of subscriptions to fixed partitions**, and let scrolling and streaming change which
subscriptions it holds rather than what any of them mean.

### The partition

Segments are append-only within a scope, so an index assigned at projection time is stable forever.
Give each segment a monotone `segment_index` and cut it into pages of a fixed size `P`:

```sql
-- page k, for every reader of this conversation, for all time
thread_id = $1 AND source_id = $2 AND projection_epoch = $3
AND entity_kind IN ('item','confirmed_input','lifecycle')
AND segment_index >= $4 AND segment_index < $5      -- $4 = k*P, $5 = (k+1)*P
```

Both bounds are multiples of `P`, so page `k`'s shape is the **same shape** on every open, for
every reader, forever. The newest page is incomplete and grows; its predicate does not change, so
Electric simply appends to that shape's log. Cursor quantization cannot do this — how many segments
a cursor range admits depends on how densely a turn packs them — which is why the index has to be
stored rather than derived.

What is _not_ paged moves out of the window shape entirely, into one shape per conversation that is
stable for the conversation's life:

```sql
thread_id = $1 AND source_id = $2 AND projection_epoch = $3
AND (entity_kind IN ('view_state','controls') OR (entity_kind = 'command' AND pending = TRUE))
```

Today these ride inside every window shape, so a rotation re-sends the view state and every pending
command along with everything else. They are not tail-scoped and never were.

Content pages mirror entity pages over the chunk table, which means the chunk rows carry their
owner's `segment_index`:

```sql
thread_id = $1 AND source_id = $2 AND projection_epoch = $3
AND owner_segment_index >= $4 AND owner_segment_index < $5
AND field IN (…)
```

### The flows, as requests

`P = 50` below for concreteness; sizing it is an open question (§ What this costs). Entity and
control shapes are mutable, so they bootstrap `log=changes_only` through a subset snapshot; content
is append-only, so it replays `log=full` from `offset=-1`.

**Open at the tail.** One resolve, then a fixed number of subscriptions whatever the conversation
holds:

```text
GET /sync/interest                      → {scope, latest_page: k, latest_index}
GET /sync/entities?page=k&offset=now     → handle Hk, offset Ok
GET /sync/entities?page=k&offset=Ok&handle=Hk&subset__where=true = true   → rows
GET /sync/entities?page=k&offset=…&handle=Hk&live=true                    → long poll
  … the same three for page k-1, so the tail is 50–100 segments rather than 0–50
GET /sync/controls?offset=now → … → subset → live                         (3)
GET /sync/content?page=k&offset=-1       → every body of page k
GET /sync/content?page=k&offset=…&live=true                               → long poll
  … the same two for page k-1
```

Nine requests and five long polls, constant in conversation size — against today's four plus two
per rendered body. Every one of those shapes is shared with every other reader of this conversation
and with this reader's next open, which is what the current per-view bounds give up.

**New items stream in.** Zero requests, **including across a page boundary**. A new segment's index
is inside the predicate of a page the reader already holds — page `k` normally, page `k+1` when the
conversation crosses into it, because the reader keeps the next page subscribed before it has any
rows (§ Crossing a page boundary). Its body arrives on that page's content poll the same way. What
the crossing costs is one new landing-pad subscription afterwards, off the critical path: **once per
`P` segments**, not once per segment, and the shape it opens is then warm for everyone.

**Scroll up.** Subscribe to page `k-2`. Three requests plus two, and **nothing already on screen is
touched**: no predicate moves, no shape is retired, no rows are withdrawn and re-delivered. The
reader holds pages `k-2 … k` and can keep going down. This is the flow that today replaces the
window and relies on re-finding a `[data-conversation-anchor]` afterwards; here there is nothing to
restore because nothing moved.

**Scroll up, then an item streams in.** The two are now independent. The new segment lands in page
`k`; the history pages are untouched; no threshold is crossed because there is no threshold. Flow 4
stops existing rather than getting a fix.

**Rotation.** Also stops existing. Bounding what a reader holds becomes a client-side decision to
**unsubscribe** from pages far off screen — which frees browser memory, disturbs nothing on screen,
and leaves the server's shapes for whoever wants them next. The server keeps no per-reader window
to expire, so `ConversationInterestExpiredError` and the 60-segment threshold both go.

**The connection drops and comes back.** Each page resumes itself:
`GET /sync/entities?page=k&offset=<last>&handle=<Hk>&live=true`. Nothing is rebuilt, so nothing
blanks — which is the whole of flow 6's requirement. A handle the server can no longer serve from
answers 409 `must-refetch`, and then **that one page** re-snapshots while every other page keeps
its rows.

**The scope is replaced.** Every shape is scoped by `projection_epoch`, so the proxy 410s them all;
the reader resolves a new interest and subscribes to the new epoch's pages behind the existing
hidden double-buffer. Unchanged from today, and still covered by
<../../debug/conversation_acceptance.md>.

### Crossing a page boundary

This is the one discontinuity the partition has, so it is worth following in full rather than
asserting it is cheap.

A segment whose index reaches `k*P` is **not** in page `k-1`'s predicate. It lands in page `k`'s
shape — which a reader watching the tail is not subscribed to, and so does not see arrive. Left
there, every `P`th segment would stall behind a subscription the reader only knows to make once it
learns the segment exists.

**So a reader holds the next page before it exists.** Electric serves a shape whose predicate
currently matches nothing as an ordinary empty shape, so subscribing to page `k+1` costs a handle
and no data. The crossing then delivers the segment into an **already-open** poll, with no request
and no stall — and the reader opens page `k+2` as the new landing pad afterwards, off the critical
path. A reader at the tail therefore holds `k-1, k, k+1` rather than `k-1, k`: seven shapes with
their content pages and the control shape, against five.

The alternative — learn the crossing from `view_state` and then subscribe — costs a round trip at
every boundary, during which the newest segment is known to exist and cannot be rendered. At
`P = 50` and a busy turn that is a visible hiccup every minute or two, to save one shape that is
shared by every reader of the conversation. The landing pad is worth it.

**Pages stay closed at both ends.** An open-ended newest page (`segment_index >= k*P`, no upper
bound) would need sealing when the next one opens, and sealing is a predicate change — a new shape,
which is the churn this whole partition exists to avoid. Closed from the start means page `k` is
complete and immutable the moment the conversation moves past it, which is exactly when it becomes
worth caching.

**What the boundary exposes, and the partition has to answer:** a batch can project segments on both
sides of it. They commit in one transaction but land in two shapes with independent logs, so a
reader can see the page-`k` head before the page-`k-1` tail. Within a page this is already true and
already handled — order by `segment_index` and let a gap fill.

What does not survive unchanged is the **catch-up gate**. Today a reader compares one collection's
`view_state.revision_cursor` against the interest's `through_cursor`, and that single check is
exactly what forces the whole-window rebuild it sits on. Under the partition, "caught up" is
composite: every subscribed page reports up-to-date, **and** the highest `segment_index` held
reaches the latest the projection has. That second half means `view_state` has to carry
`latest_segment_index` — otherwise a reader cannot tell "no more segments" from "the page carrying
them is still catching up". It is more moving parts than one comparison, and it is checkable per
page rather than all-or-nothing, which is what lets a page arrive without the rest of the view
waiting on it.

### What this costs

- **Shapes.** A conversation costs one control shape plus two per page a reader has open, and a
  reader at the tail holds three pages — the one it is reading, the one before it, and the empty
  landing pad above it. So **seven** at the tail, and twenty-three after scrolling back 500
  segments at `P = 50`. Against `ELECTRIC_MAX_SHAPES=1024`, shared across readers and opens, where
  today's per-view bounds are shared with nobody. **`P` has to be sized against that budget before
  this is built** — and note the budget is global, so the ceiling is concurrent conversations times
  pages held, not one conversation's depth. The shape-creation log (§ W7, landed) is the
  instrument: count distinct handles over a session.
- **Three schema additions**, all at projection time: `segment_index` on the entity rows,
  `owner_segment_index` on the chunk rows, and `latest_segment_index` on `view_state`, which is what
  lets a reader tell "there are no more segments" from "the page holding them has not caught up".
  The first two are append-only facts, so neither can drift.
- **Nine requests on open rather than four.** The trade is deliberate: constant either way, and
  these are cache hits for the second reader where the current four are not.

### What this does not answer

- **Whether a page's content should carry reasoning.** Orthogonal to partitioning, and open —
  reasoning writes to `text` and renders behind a disclosure (§ W9).
- **Per-card readiness.** A page collection is a smaller and more local unit than one window
  collection, but a page still has no signal for "this card's body has arrived". Whatever waited on
  a card being mounted — a visual gate, a test — needs something explicit to wait on either way.
- **How a reader decides to unsubscribe.** Bounding held pages is now a client policy with no
  server contract behind it, which is simpler but is not free of judgement.

## W9 and W5, as they stand after the flows

Both were written before the flows were, and the flows overtake them. Kept here for the parts that
survive, and marked where they do not.

### W5 — superseded, and promoted

W5 identified the defect correctly: `conversation_entity_interest` sets `tail_from` to the
30th-largest segment cursor, so appending one segment makes the 30th-largest what was the 29th — a
new bound, a new shape definition, a new handle. An idle conversation reuses its shape across opens;
a growing one defines a fresh one on every open, and rotation defines another. A shape _per
conversation_ is ordinary; a shape per _view of_ a conversation is what this builds.

It proposed the page-aligned bound over a stored per-segment index, and then called it **"hygiene
rather than latency"** and deferred it. That judgement is now wrong. The flows show the moving bound
is not a cache-efficiency footnote — it is what makes paging replace the window, what makes rotation
exist, what opens flow 4's hole, and what turns a dropped connection into a rebuild. The partition
is the design, not its tidying, and it is written out in full above.

One rejection recorded there still holds: **one shape per thread**, the whole conversation's
metadata stable forever, was considered and rejected because opening a conversation loads its tail,
not its history. That is a product decision and independent of everything here — paging keeps it.

### W9 — the content shape, at page granularity

W9's premise stands: the bodies a reader renders are the items it renders, so they belong in a shape
alongside them rather than one shape per body, and the open path must not be `O(bodies)` round
trips. What changes is the bound. W9 proposed `owner_cursor >= tail_from` — the interest's own
moving bound, and so an inheritance of exactly the defect W5 names. Content pages carry the same
content under a bound that does not move.

Three pieces of it are independent of the partition and survive intact:

- **The extent on `PayloadRef`** — `content_bytes`, the value's whole length at that revision. A
  shape carrying many bodies hands a reader more than the revision its metadata names, so each body
  needs a bound it can apply without a second request. This is a **consistency requirement, not an
  optimisation**: `test_http_admission_ahead_of_replay_does_not_skip_earlier_events` holds
  `/sync/entities` while leaving the body route alone, and a reader with no bound renders chunks the
  held metadata has not reached. Contiguity from index 0 gives the whole value at _some_ revision of
  the generation, which is not the one being shown. A chunk count beside it would be worse than
  redundant: the byte length is a projection fact, while how many chunks a value occupies depends on
  how the store batched its writes, so the two can disagree.
- **The shape per generation, not per revision**, for the bodies that stay individually read. A
  `chunk_index < n` bound makes shape identity depend on the revision, so every append defines a new
  shape and a completing item pays a cold creation for bytes its own stream already delivered.
- **`follow` is not a thing the model has.** It is derived from `live && completion === null`, so a
  completed item is treated as final, while the model says existing items can change anywhere in the
  history. With an extent there is nothing to derive: a reader renders the prefix its own reference
  covers, and a short prefix is what streaming looks like.

One piece is still **undecided**, and the partition does not decide it: whether a page's content
carries reasoning. Reasoning writes to `text` and renders behind a `RetainedDisclosure`, exactly
like tool arguments and output, and a trace is routinely longer than the answer it precedes. The
criterion — what the page always renders — excludes it, and the item `kind` cannot express that,
because the predicate selects over the chunk table, which has no kind. Splitting
`PayloadField.REASONING` is what would let a page carry the one and not the other.

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
- Distinct Electric shape handles created during one turn: zero new shapes per completing item, and
  zero per arriving segment while a reader holds a history page. The second is flow 4, and the
  shape-creation log (§ W7, landed) reports a handle per request, so counting distinct handles over
  a session answers both.
- **`P` against `ELECTRIC_MAX_SHAPES=1024`**, before the partition is built rather than after: how
  many pages a plausible set of concurrent readers holds, given that pages are shared between them.
- Browser coverage for the two flows nothing asserts end to end: scroll up and then stream (no hole
  appears, and the reader's place does not move), and a page's rows surviving an unsubscribe of a
  page above it.
- Small-file create/fsync latency on `seaweedfs-ovh` versus `local-path-ovh-ssd` from the Electric
  pod's node, before spending W1's PVC change.

## Out of scope

- Replacing Electric. The protocol usage is valid; the granularity is wrong.
- A longer-lived `Cache-Control` on payload bodies, for the logout reason above.
- The open acceptance gates in `agentplane/debug/conversation_acceptance.md`. Latency work does not
  close them and they do not block it.
