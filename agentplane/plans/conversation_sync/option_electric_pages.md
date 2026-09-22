# Option: TanStack DB + ElectricSQL, partitioned by pages

Electric's primitives are in <electric_primitives.md>; what the deployed viewport design does with
them, and how it fails, is in <option_electric_today.md>. This file is the repair: keep the stack,
change the partition.

**The one idea.** Every failure of the deployed design — rotation, paging replacing the window, a
dropped stream rebuilding the selection — is the same failure: **redefining a shape a reader is
looking at**. A shape _is_ its predicate, so each of those is a new server-side shape, a new
snapshot, and rows that go away and come back. The repair is not to make redefinition cheaper. It
is to stop redefining: make a reader's view an **additive set of subscriptions to fixed
partitions**, and let scrolling and streaming change which it holds rather than what any of them
means.

**Against the requirements** (<requirements.md>): it satisfies **D1** — pages never overlap and
never move, so scrolling up subscribes to a page the client does not hold and re-sends nothing —
and it can satisfy **P8** by giving each payload field its own content shape per page, letting the
client subscribe to the ones it wants. It has no hard blocker. Its case against is cumulative:
shape count against `ELECTRIC_MAX_SHAPES`, the page boundary and its landing pad, a catch-up signal
that becomes composite, and no per-card readiness signal.

**Numbering note:** `W5` and `W9` appear below as work items from the draft this grew out of. They
are not the `D` desires in <requirements.md>.

The flows above fail in one place — every one of them, including the two that work, turns on
**redefining a shape while a reader is looking at it**. Rotation moves `tail_from`; paging up
replaces the window; a dropped stream rebuilds the selection. Since a shape _is_ its predicate
(<electric_primitives.md>), each of those is a new server-side shape, a new snapshot and a reader
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
rows (§ Crossing a page boundary, below). Its body arrives on that page's content poll the same way. What
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

#### Watching the tail while reading elsewhere

S4's permission fits this option without new mechanism: a reader scrolled into history keeps the
tail page and the control shape subscribed alongside its history pages. The control shape already
carries `view_state`, so "the conversation is moving" arrives whether or not the reader is looking.

S4 itself holds because a page's shape delivers every change to a row in that page, regardless of
when the row was first written — an edit to an old segment in a page the reader holds arrives on
that page's poll. The risk is the opposite one: a reader that has **unsubscribed** from a page to
bound its shape count will not hear about edits there. That is correct (it is not showing them) but
it means the unsubscribe policy is also a correctness surface, not purely a memory one.

## What this costs

- **Shapes.** A conversation costs one control shape plus two per page a reader has open, and a
  reader at the tail holds three pages — the one it is reading, the one before it, and the empty
  landing pad above it. So **seven** at the tail, and twenty-three after scrolling back 500
  segments at `P = 50`. Against `ELECTRIC_MAX_SHAPES=1024`, shared across readers and opens, where
  today's per-view bounds are shared with nobody. **`P` has to be sized against that budget before
  this is built** — and note the budget is global, so the ceiling is concurrent conversations times
  pages held, not one conversation's depth. The shape-creation log (shape-creation logging, landed in #7589) is the
  instrument: count distinct handles over a session.
- **Three schema additions**, all at projection time: `segment_index` on the entity rows,
  `owner_segment_index` on the chunk rows, and `latest_segment_index` on `view_state`, which is what
  lets a reader tell "there are no more segments" from "the page holding them has not caught up".
  The first two are append-only facts, so neither can drift.
- **Nine requests on open rather than four.** The trade is deliberate: constant either way, and
  these are cache hits for the second reader where the current four are not.

### What this does not answer

- **Whether a page's content should carry reasoning.** Orthogonal to partitioning, and open —
  reasoning writes to `text` and renders behind a disclosure, so a shape on `field = 'text'` carries
  every reasoning trace in the window unless the field is split.
- **Per-card readiness.** A page collection is a smaller and more local unit than one window
  collection, but a page still has no signal for "this card's body has arrived". Whatever waited on
  a card being mounted — a visual gate, a test — needs something explicit to wait on either way.
- **How a reader decides to unsubscribe.** Bounding held pages is now a client policy with no
  server contract behind it, which is simpler but is not free of judgement.

## What the partition does not decide

Two pieces of the earlier work are independent of how the conversation is partitioned, and are
worth keeping whatever wins:

- **The extent on `PayloadRef`** — `content_bytes`, the value's whole length at that revision. A
  shape carrying many bodies hands a reader more than the revision its metadata names, so a body
  needs a bound it can apply without a second request. This is **S1**, a consistency requirement
  rather than an optimisation: `test_http_admission_ahead_of_replay_does_not_skip_earlier_events`
  holds `/sync/entities` while leaving the body route alone, and a reader with no bound renders
  chunks the held metadata has not reached. A chunk count beside it would be worse than redundant —
  byte length is a projection fact, while chunk count depends on how the store batched its writes.
- **`segment_index` on the fold.** Needed by anything that pages, and a cursor cannot substitute:
  how many segments a cursor range covers depends on how densely a turn packs them.

And one that is still open: **`follow` is not something the conversation model has.** It is derived
from `live && completion === null`, so a completed item is treated as final while the model says
items can change anywhere in history. With an extent there is nothing to derive — a reader renders
the prefix its own reference covers, and a short prefix is what streaming looks like.
