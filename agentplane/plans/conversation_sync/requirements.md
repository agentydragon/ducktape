# Conversation sync — what a design has to do

Stable IDs so options and the fit matrix can cite them. Each says where it comes from, because a
requirement nobody can source is a preference and should be argued as one.

## Constraints — not traded away, assumed by every option

| ID  | Constraint                                                                                                                          | Source                                   |
| --- | ----------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| C1  | The conversation fold is **materialized in Postgres**. Options differ in how a browser learns about it, not in whether it exists.   | Owner, 2026-09-22; already built         |
| C2  | Every read is **authorized by the app**, and no browser reaches a sync engine directly. About auth, not about who picks the window. | `electric.py`; the proxy exists for this |
| C3  | Deployed state is **disposable** — a schema or epoch change resets staging and testing rather than migrating.                       | <../../../AGENTS.md> § Refactoring       |
| C4  | The runner event log is the source of truth; the fold is derived and rebuildable under a new `projection_epoch`.                    | <../../docs/thread_view_sync.md>         |

**C2 is narrower than the deployed code treats it as.** The app must ensure a reader only reads
threads it is entitled to, and must keep a browser from talking to Electric directly. It does _not_
require the server to dictate the reader's window. The deployed proxy resolves the interest
server-side and refuses a client's bounds if they disagree — stricter than auth needs, and not an
argument against a client-declared window (**D1**, **P8**). A client asking for segments 50–150 of a
thread it may read is asking for nothing it is not entitled to.

What C2 does still forbid: a bound reaching **outside the authorized thread**, an unbounded request
letting one reader pull arbitrary volume, and any path that skips the app.

## Product behaviour

| ID  | Requirement                                                                                                                                                                                | Source                                                  |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------- |
| P1  | Opening a conversation shows its **tail** quickly — not its history.                                                                                                                       | Product decision, recorded 2026-09-22                   |
| P2  | New items and revisions appear **live**, with no user action.                                                                                                                              | Deployed behaviour                                      |
| P3  | An existing item can change **anywhere in history**, not only at the tail.                                                                                                                 | <../../docs/thread_view_sync.md>                        |
| P4  | A reader can scroll back **arbitrarily far**, incrementally, bounded work per step.                                                                                                        | Deployed behaviour                                      |
| P5  | The reader's **place survives every sync event**: no DOM teardown, no scroll jump, nothing already shown withdrawn.                                                                        | Owner, 2026-09-22                                       |
| P6  | A **transient disconnect** keeps what is on screen and resumes without refetching it.                                                                                                      | Owner, 2026-09-22; `test_projected_browser…` asserts it |
| P7  | A **stale-epoch read is refused, never served.** A rebuild may cost the reader a full reload. Below.                                                                                       | <../../debug/conversation_acceptance.md>                |
| P8  | **The client chooses its content selection** — text, reasoning, tool arguments, output — and that choice composes with streaming. Selecting nothing still returns metadata and references. | <../../docs/thread_view_sync.md> § Queries              |
| P9  | Pending and optimistic commands reconcile after a lost reply.                                                                                                                              | Deployed behaviour                                      |

**P8 is what the deployed design breaks, and it is not hard to satisfy.** The spec makes content
selection the client's; the deployed implementation has no parameter for it, and the page design
put a fixed field set in a shape predicate. Baking "reasoning is lazy, text is eager" into the
protocol is an **optimisation** on the wrong side of the wire.

It is satisfiable everywhere, including with Electric. `field` is a column of the chunk table, so a
field selection is a `where` predicate and therefore part of a shape's identity — which means **one
shape per field**, and the client chooses by choosing which of them to subscribe to:

- A reader always holds the entity shapes, which carry metadata and references. "Selecting nothing
  still returns metadata and references" holds by default.
- It subscribes to the `text` content shape because it always renders text; to `reasoning` only if
  it wants reasoning; to `output` when a disclosure opens, or from the start if it wants it
  streamed. **That is the client choosing, and it composes with streaming** — every content shape is
  live like any other.
- Per-**field** shapes share better than per-selection ones: a reader wanting `{text}` and one
  wanting `{text, reasoning}` share the `text` shape, where `field IN ('text')` and
  `field IN ('text','reasoning')` would be two shapes with overlapping contents.

What it costs is shape count: pages × fields in use, rather than pages. That is an **O1** problem
against `ELECTRIC_MAX_SHAPES`, not a P8 problem.

So P8 separates designs that have a parameter for it from designs that do not, and every design
here can grow one.

**P7, and why it is cheap.** The fold can be **rebuilt** — reprojected from the event log under a
new `projection_epoch`, which is part of the identity of every entity, chunk and payload reference.
Old and new cannot be mixed: cursors and references do not correspond across a rebuild.

**The epoch exists for forward compatibility**, not for a runtime event. Nothing at runtime mints
one: `CONVERSATION_PROJECTION_EPOCH` is a module constant stamped when a thread's projection is
first created, and on every later batch a mismatch **raises** rather than reprojecting.

```python
if checkpoint.projection_epoch != CONVERSATION_PROJECTION_EPOCH:
    raise ConversationProjectionError(f"… must be reset for {CONVERSATION_PROJECTION_EPOCH!r}")
```

So the only cause is a deploy that changed the constant, because the projector's output shape
changed. The app then stops ingesting that thread until someone resets it; no rebuild path is
implemented, and under **C3** the standing answer is to reset staging and testing, discarding the
data rather than swapping it under a reader. `source_id`, the other half of a scope, is not a
rebuild either — a source change stops the browser rather than re-scoping it.

**What P7 therefore requires is only the refusal.** A read carrying a stale epoch must get a 410
rather than data from the new one — that is the forward-compatibility guarantee, and it is what
stops a projector change from silently serving mixed-shape rows. **A full page reload afterwards is
acceptable**, because this happens essentially never.

**What it does not require: a seamless swap.** The deployed implementation does more — it syncs the
new epoch in a hidden subtree and swaps it in only once caught up, preserving an unsent draft and
collapsed disclosures
(`test_projection_epoch_replacement_retires_old_requests_and_preserves_draft`,
<../../debug/conversation_acceptance.md>). That is nicer than required, and it is not free: the
pending-selection double buffer it needs is the same machinery rotation and paging up use. **A
replacement design need not reproduce it.** In a design that never rebuilds a selection, nothing
else needs that machinery either, and it can go.

## Sync semantics

| ID  | Requirement                                                                                                                                | Source                                 |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------- |
| S1  | A body never renders content its **own reference does not name** — no showing a later revision's bytes under an older revision's metadata. | `test_http_admission_ahead_of_replay…` |
| S2  | Segments render in **conversation order** whatever order they arrive in.                                                                   | Implied by P2/P4                       |
| S3  | A reader can distinguish **"caught up"** from "still arriving", per whatever unit it subscribes in.                                        | `view_state` catch-up gate             |
| S4  | A revision to an item the reader **currently holds** always reaches it. No silent staleness.                                               | P3 + P2                                |

**S4, and the assumption it exists to forbid.** Conversations are _usually_ edited near their tail.
That is an observation about traffic and **must not become an assumption in the protocol**: an edit
to a message in the middle of a window a reader is looking at is delivered on the same terms as an
edit to the last one. Nothing may be dropped on the floor because it was old.

The concrete trap is that **`segment_index` and `revision_cursor` are independent axes**. A segment
written long ago and edited just now has a _low_ index and a _high_ revision. Any delta must filter
on both — the window by index, the freshness by revision — and an implementation that conflates them
into one "everything after cursor X" silently implements the tail-only assumption. Recency is not
position.

Things that would bake it in, none of which are allowed: scanning only the last _K_ rows for
changes; a changes feed that retains only recent entries; ordering a window query by revision and
truncating it.

**Explicitly permitted:** a reader may stay subscribed to the **tail** even while looking somewhere
else, so it can tell that the conversation is moving and keep `view_state` current. That is a second
watch alongside the window, which D1 does not charge for. It is a permission, not a requirement — an
option that does not need it is not worse for that.

## Efficiency

| ID  | Requirement                                                                                                                           | Source                          |
| --- | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| E1  | Requests on open are **O(1)** in conversation size. Today they are `O(bodies)`, which is the reported defect.                         | Measured 2026-09-22 (§ README)  |
| E2  | Bytes on open are bounded by **what is rendered**, not by conversation length.                                                        | P1                              |
| E3  | Streaming adds **≈0 requests** per arriving item.                                                                                     | Measured: ~0.4 s per round trip |
| E4  | Scrolling back loads **only the new page** — not the window a reader already holds.                                                   | Owner, 2026-09-22               |
| E5  | Nothing already held is **re-transferred**; revalidation over retransfer.                                                             | E2/E4                           |
| E6  | **No timer polling.** Updates arrive over a held connection — WebSocket, SSE or long poll — never a request re-issued on an interval. | Owner, 2026-09-23               |

**E6 allows a long poll.** A request the server holds until something changes, and the client
re-issues when it returns, is waiting on a change rather than on a clock. What it rules out is a
request sent every _n_ seconds whether or not anything changed: an idle reader makes no requests.

## Operability

| ID  | Requirement                                                                                               | Source                            |
| --- | --------------------------------------------------------------------------------------------------------- | --------------------------------- |
| O1  | Per-reader server state is **bounded**, and preferably **shared** between readers of one conversation.    | `ELECTRIC_MAX_SHAPES=1024`        |
| O2  | The sync tier scales **horizontally**; staging runs two app replicas.                                     | <../../app/README.md>             |
| O3  | **Debuggable**: what a client is subscribed to, and why it received a given row, is answerable from logs. | Owner, implied by this whole plan |
| O4  | **Few moving parts.** An option that needs a new engine owes an argument that the problem needs one.      | Owner, 2026-09-22                 |

## Desires, not requirements

Weighed, not required. Lettered `D` because option_electric_pages.md uses `W1`…`W9` for the work
items of an earlier draft, and two numbering schemes in one directory is how a citation comes to
mean the wrong thing.

**These do not disqualify anything.** An option that fails a desire owes an argument that what it
wins elsewhere is worth more — not an exit from the comparison.

### D1 — moving the window never re-sends what the client holds

The invariant, in the owner's words: closing the previous watch and opening `O(1)` new ones is
fine — **the new watch must not send the client data it already has.**

The worked case. A client holds segments 100–200 at revision 9932. The reader scrolls up, so the
client now wants 50–150. It should fetch **50–99**, learn of any change to **100–150 since revision
9932**, and drop 151–200. What it must not do is receive 100–150 again.

Note what this is _not_: a cap on subscriptions. The count is free; the re-transfer is the cost.

**How it splits the options:**

- **Overlapping windows re-send.** Any design where the viewport is one predicate that changes —
  Electric as deployed, or a delta poll without a `have` parameter — replays the new window whole.
  Electric as deployed has no choice about it: the window is in the shape's predicate, which is
  fixed at creation, so a moved range is a different shape. One shape per thread avoids that by
  reading the window as subset snapshots of a shape whose predicate never moves
  (§ option_electric_subsets).
- **Non-overlapping partitions do not.** The page partition (§ option_electric_pages) never moves a
  bound, so scrolling up subscribes to a page the client does not hold and re-sends nothing. It pays
  for this in subscription count, which D1 does not charge for.
- **A stated `have` does not either.** § option_moving_window sends the difference because the
  client says what it holds.

One honest edge: a reader that unsubscribes from a page, scrolls back to it and re-subscribes does
re-download it. That is not a D1 violation — the client dropped the data — but it is the cost of
bounding held pages, and a client that keeps them cached avoids it.

- **D2 — no seam between windowed and on-demand content.** Whether a body streams or is fetched
  should be one mechanism with a parameter, not two code paths that behave differently. P8's
  implementation-side twin.
- **D3 — incrementally reachable.** A design we can get to in steps, each shippable and better than
  the last, beats one that has to land whole.
- **D4 — streaming costs the delta.** Tokens appended to a body cost the client traffic in
  proportion to what was appended, not a re-send of the prefix it already holds. Owner, 2026-09-23.

**D4 is mostly given by the storage.** A body is insert-only chunks: each ingestion batch that
appends to a body writes one new chunk row holding only that batch's text
(`agent_runtime/view/payloads.py`). A design that syncs chunk rows therefore transfers the delta,
at batch rather than token granularity. What fails D4 is re-sending a body whole on each change —
A0, or an engine that re-sends a whole value or query result. Two costs ride on each append
regardless: the entity row whose reference moved (whole, under Electric's `replica=full`), and a
replacement, which starts a new generation and so is a new body.
