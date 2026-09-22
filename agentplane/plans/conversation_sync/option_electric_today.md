# Option: keep what is deployed

The baseline that already exists, recorded so "do nothing" and "fix the worst of it" are on the
matrix rather than assumed away.

An interest is resolved per reader — `(anchor_cursor, tail_from, window_from?, window_before?)` —
and becomes an Electric shape over `conversation_entity`. Each rendered body is a second shape,
reached through a `payload-interest` call that hands the client its extent.

## What it gets right

**P1, P2, P3, P7, P9, S1, S2, S4, O2** all hold today, and `test_thread_browser` covers most of
them.

The epoch double-buffer is worth a note, because it looks like an asset and is not one. It is
genuinely good — the pending selection syncs in a hidden subtree and swaps only once caught up, so
the visible tree never blanks — but P7 asks only that a stale read be refused, and a rebuild may
cost a full reload (<requirements.md> § P7). So this machinery exceeds its requirement, and the same
machinery is what rotation and paging up use, which is where P5 is lost. It is not a reason to keep
this design.

## What it gets wrong, in order

- **E1** — the open path is `O(bodies)`: two round trips each, ~60 for a 30-segment tail. This is
  the reported twenty seconds, and it is the reason this plan exists.
- **P5, E4, E5** — paging up re-resolves the interest, which changes the shape's predicate, which is
  a different shape. The reader's whole window is re-snapshotted, and its place is restored
  afterwards by finding `[data-conversation-anchor]` and correcting `scrollTop`.
- **P5 again, via rotation** — a client-side threshold at 60 segments and a server-side
  `ConversationInterestExpiredError` at the same count both rebuild the selection. With a history
  page open the count sits at 59, so streaming trips it every couple of segments and opens a hole in
  the middle of the view (see option_electric_pages.md § flow 4).
- **P8** — the content selection is not the client's. There is no parameter for it, and unlike the
  page design this one has no obvious place to put one.
- **D1** — every change of window is a new shape replaying from `offset=-1`, so paging up re-sends
  the part of the window the reader already has. The page partition fixes this; moving the interest
  predicate is what breaks it.
- **O1** — the bound moves with every appended segment, so no two readers and no two opens of one
  conversation share a shape. The 1024-shape LRU churns behind a cache nobody hits.

## Fixing it in place

Two of these have local fixes that do not need a new design: the `payload-interest` round trip can
go (the extent belongs on the reference), and the per-revision chunk bound can go (bound the shape
to the generation). Those are worth taking whatever else is decided — they are in
<option_electric_pages.md> § What the partition does not decide, and #7592 implements them.

The rest — P5, E4, E5, O1 — are consequences of a bound that moves, and no local fix reaches them.

## What the open path does today

1. `GET /sync/interest` — the projection checkpoint and the 30th-last segment cursor.
2. `GET /sync/entities` — an Electric shape over `cursor >= tail_from`.
3. `GET /sync/payload-interest` — per rendered body, its manifest's `chunk_count`/`content_bytes`.
4. `GET /sync/payload-chunks` — per rendered body, a second Electric shape.

Nothing overlaps: `ConversationCollection` mounts no body until the entity shape has caught up.

## The flows, as deployed

What a reader actually does, from the gesture to the requests to what the DOM ends up holding.
This is where the failures above come from, and it is the checklist any replacement inherits.

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

A window-scoped content shape (#7592) makes the cost worse without touching the cause: a rotation
would rebuild two shapes instead of one, and the content shape replays from `offset=-1`, so the
window's whole text re-transfers each time. Eager window content is the right trade for an open and
the wrong one for a per-segment rebuild.

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

Against that it costs what a moving bound always costs — a new shape definition, a new handle, no
reuse between opens or readers — and it spends a scroll restoration each time. So the
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
- Flow 6 is covered: `test_projected_browser_streams_runner_events_and_loads_bodies_lazily` takes
  the browser offline and asserts the visible text survives. What this design does about it is the
  open question, not whether anyone checks.
- Flow 3's place-keeping is measure-and-correct against `[data-conversation-anchor]`, which is only
  needed because the window is rebuilt underneath the reader. Under a partition that is never
  rebuilt there is nothing to restore, and that machinery can go.
