# Conversation sync latency

Opening a Thread on staging took tens of seconds before any conversation text painted, and text
that had already arrived rendered much later than its response. A HAR showed several requests
spending ~20 s upstream while their bodies transferred in milliseconds, so the cost was
server-side shape work rather than transfer or React.

Reading the integration found three independent causes. Two are fixed; this plan burns down once
the remaining two items land and the measurement gates pass.

## What the open path cost

Four stages that could not overlap, because `ConversationCollection` mounts no body until the
entity shape catches up (`onRows(caughtUp ? rows : [])`):

1. `GET /sync/interest` — the projection checkpoint and the 30th-last segment cursor.
2. `GET /sync/entities` — an Electric shape over `cursor >= tail_from`.
3. `GET /sync/payload-interest` — per mounted body, the manifest's `chunk_count`/`content_bytes`.
4. `GET /sync/payload-chunks` — per mounted body, a second Electric shape.

A 30-segment tail cost one entity shape plus up to 30 payload shapes. Stages 3 and 4 are gone.

## Fixed

**Shape identity no longer fragments per revision.** `ElectricProxy.payload_chunks` bound
`chunk_index < $8` to the selected revision's `chunk_count`, and bound parameters are part of a
shape's definition, so every revision of a field was a distinct shape. The bound was redundant:
chunks are append-only within a generation (a replacement mints a new one at
`conversation_projection.py`'s `base.generation if append and base else cursor`), and
`ActivePayloadBody` already filtered the prefix client-side. The chunk route now names a
generation and no revision at all, and the browser's collection is keyed the same way, so an
advancing revision cannot rebuild it. The visible symptom this removes: an item completing used
to drop the `follow` collection whose bytes had already arrived and pay a cold shape creation to
fetch the same text back.

**Completed bodies leave Electric.** `GET /threads/{id}/conversation/payload` returns one
immutable field revision whole, assembled from its generation's chunks, with a strong `ETag` and
`private, no-cache` so a reader revalidates rather than retransfers. `PayloadBody` splits into a
streaming path (the generation's chunk shape, rendering the contiguous prefix from index 0) and a
completed path (that one request). The contiguous prefix carries across the handoff, so arrived
text is never withdrawn to load the value already on screen.

**The render gate's round trip is gone.** Rendering waited on a `content_bytes` from a separate
`payload-interest` fetch, repeated per delta per visible streaming body because `referenceKey`
included `revision_cursor`. A completed body now arrives whole, and a streaming one is complete at
its current revision by contiguity, so neither needs an extent. `/sync/payload-interest`,
`ConversationPayloadSelection` and `conversation_payload_selection` are deleted.

**The debug observations page no longer ships raw entries.** The list query selects identity and
kind only; `GET /threads/{id}/conversation/observations/{cursor}` returns one entry when a reader
expands it.

**Shape creation is measurable.** `ElectricProxy._forward` logs the upstream duration, status and
`electric-handle` around `send(..., stream=True)`, which returns on headers — so a cold creation
is now distinguishable from a warm snapshot, which a HAR cannot show.

## Open

### W1 — Electric shape storage is on distributed HDD

`cluster/cdk8s/agentplane/electric.py` sets `ELECTRIC_STORAGE=fast_file` with
`ELECTRIC_STORAGE_DIR` on a 5 GiB `seaweedfs-ovh` PVC: "SeaweedFS CSI (RWX, distributed),
media-blind (HDD bulk)", a FUSE mount over network storage. Shape creation writes a per-shape
directory of small files and the `ELECTRIC_MAX_SHAPES` LRU deletes them once a minute. Unlike the
churn above, this taxes every shape creation however few remain.

Moving the PVC to `local-path-ovh-ssd` (NVMe, zone `hil-ovh`, `WaitForFirstConsumer`) needs no
other change: the Deployment is already `replicas=1` with `strategy=recreate()` and
`attract_to_zone`. Electric's shape storage is a rebuildable cache — the acceptance matrix records
a forced slot loss recovering automatically with old handles answering 409/must-refetch — so the
cost is that a node drain loses it and clients take one refetch round.

**Deferred by the owner for now; the storage class stays as it is.** The fixes above cut the open
path from ~31 shape creations to one, which is what makes deferring it tolerable.

A node-local volume also pins Electric to one replica, which it already is. If a backing store
Electric can share — its own tables in PostgreSQL, or anything else that several instances can read
— turns out to exist in the pinned version, that answers both this and horizontal replication at
once and is the better shape to aim at. Which of its storage backends allow that is unchecked here;
settle it before spending the PVC change.

### W5 — the entity shape is redefined by every appended segment

This was written as a minor follow-up gated on W1's measurement. Reading the resolver closely says
otherwise, and it is now the largest remaining cost on the open path.

`conversation_entity_interest` sets `tail_from` to the **30th-largest segment cursor** at or below
the checkpoint (`lower(anchor + 1)`), and that value is the shape's `$4`. Append one segment and
the 30th-largest becomes what was the 29th — a different bound, a different shape definition, a new
handle. So an idle conversation reuses its shape across opens, while **a growing one defines a
fresh shape on every open**, and rotation at `segmentCount > 60` defines another. With W1 deferred,
each of those is a cold creation on the slow volume, which matches a report of delays clustered
around an active conversation rather than a dormant one.

The framing matters more than the arithmetic. **An Electric shape is a partition, not a viewport.**
It is a server-side cache with a log, maintained from the replication stream, shared by every reader
whose interest matches it and meant to outlive any one of them — which is why the pinned deployment
caps how many may exist at once and evicts by use. A predicate that embeds a continuously moving
bound gives up all of that: no reuse between two readers of the same conversation, no reuse between
two opens by the same reader, and an eviction queue churning behind both. A shape _per conversation_
is an ordinary Electric pattern; a shape per _view of_ a conversation is not, and that is what
`cursor >= <30th-last>` builds.

Two ways to stop treating it as a viewport:

- **One shape per thread.** Predicate `thread_id = $1`, the whole conversation's metadata, stable
  forever and shared by every reader and every open, with Electric syncing it incrementally as
  designed. Bodies stay separately selected, which is where the bytes are. The requirement that
  opening a Thread must not replay its history was written against **raw events** — the inspection
  behind it measured 3,091 events and 1.79 MB of SSE for eight turns — and a projected entity row
  is not that: one row per segment, no payload. What makes this a real question rather than an
  obvious win is that a lifecycle row embeds its whole event as JSON, so the rows are not uniformly
  small, and a conversation's row count still grows without bound. Measure rows and bytes per
  conversation before ruling it in or out.
- **One shape per page of a thread.** Keep a bound, but make it a partition many opens share, which
  is the scheme below.

If the arithmetic below is needed at all, it has to make the bound stable without unbounding the
window:

- **Cursor quantization is not it.** Rounding `tail_from` down to a multiple of `G` is stable, but
  how many segments that admits depends on how densely cursors fall, which varies per turn — a
  tool-heavy turn packs segments together. There is no `G` both large enough to be stable and small
  enough to stay bounded, and a bounded fallback to the exact cursor restores the churn exactly
  where conversations are busiest.
- **A stored segment index is.** Segments are append-only — an item takes its first-observed cursor
  and keeps it — so a monotone per-segment index assigned at projection time is stable under
  append. The projector carries the running count on the checkpoint and stamps each new segment;
  the interest returns a **page-aligned** `segment_index` bound, so the shape admits between one
  and two pages by construction and is redefined once per page rather than once per segment. The
  history window converts its `before_cursor` to an index with one indexed lookup. It costs a
  column, a projector change and an epoch bump, and no tuning constant.

Whichever it is, **the payload shapes want the same answer**: W9's window-scoped chunk shape carries
the same bound, so a viewport predicate there would churn for the same reason. One partitioning
scheme should serve both, which is an argument for settling this before building W9.

**Commands** are a separate, much smaller case: that shape binds a sorted `entity_id IN (...)`
list, so every change to the selected set defines a new one. It is by-ID reconciliation of terminal
outcomes, which wants a lookup rather than a subscription — pending commands already stream on the
entity shape. The set changes at human pace, and the change sits on the recovery path four
lost-response cases cover, so it stays behind the entity work.

**What decided the order.** The measurement below: a cold shape creation is sub-second, so this is
not where the latency is. It stays on the list as a correctness-of-usage item, behind W9.

### W9 — collapse content back onto one mechanism

`GET /threads/{id}/conversation/payload` exists because a payload shape is scoped to one field of
one segment, so a 30-item tail wanted 30 shape creations. Leaving Electric was the wrong answer to
that; **widening the shape** is the right one. The chunk table carries `owner_cursor`, so one shape
over `owner_cursor >= tail_from AND field IN ('text','confirmed_input')` — the entity interest's own
bounds — covers every body the page renders, in one shape rather than thirty, and a separately
selected shape still serves reasoning, arguments and output when a disclosure opens them.

That also removes an assumption this integration should not be making. `projected_session.tsx`
derives `follow` from `live && completion === null`, so a completed item is treated as final. It is
not wrong today — a field that gains a revision rewrites its entity row, which arrives over Electric
and re-triggers the read — but the conversation model says existing items can change anywhere in the
history, and a design that did not distinguish "streaming" from "complete" would not need to be
argued about. On the window shape the distinction disappears: a body updates because its chunks did.

Two things block it:

- **Reasoning has no field of its own.** `PayloadField` is `text, arguments, output,
confirmed_input, command_input`, and a reasoning item writes to `text`, distinguished only by the
  item's `kind` on its entity row. A window shape on `field = 'text'` would therefore pull every
  reasoning body, which the requirements say may stay omitted until requested, and which
  `ContentSelection` already names as its own selectable kind. Adding `PayloadField.REASONING`
  changes stored rows, so it wants an epoch bump — the same one W5 needs.
- **It is only a win once a shape is cheap or rare.** Two cold shapes per open is worse than one
  plus a body read per item while creation costs what it currently costs. So this lands after W1 or
  W5, not before, and doing it first would be a regression.

## Measured, 2026-09-22

Against `agentplane-testing`, on threads whose shapes had never been created, through the deployed
app's own routes:

| Stage                                     | 7-row thread A | 7-row thread B |
| ----------------------------------------- | -------------- | -------------- |
| `/sync/interest`                          | 0.54 s         | 0.37 s         |
| `/sync/entities` (offset=now + snapshot)  | 0.89 s         | 0.81 s         |
| 3 bodies (`payload-interest` + `-chunks`) | 2.90 s         | 2.53 s         |
| **total**                                 | **4.33 s**     | **3.72 s**     |

A cold entity shape took 0.37–0.62 s and a warm one 0.36–0.53 s. **Shape creation is not the
cost** — cold and warm are indistinguishable, so what is being measured either way is a round trip.

That overturns the diagnosis this plan opened with. The open path costs **one round trip per
request and roughly 0.4 s per round trip**, and it issues two per body. Seven rows and three bodies
already cost 4.3 s; the 30-segment tail the design specifies is ~60 requests on the open path, and
~0.4 s each is the reported twenty seconds. Not one slow shape — sixty ordinary ones.

Read this as a ratio, not as absolutes: these come through an agent HTTPS proxy from outside the
cluster, so a browser's round trip is smaller. What holds regardless is that the open path is
`O(bodies)` round trips when it needs to be `O(1)`.

Consequences, in order:

- **W9 is the fix, not a follow-up.** One window-scoped chunk shape makes the open path four
  requests whatever the conversation's size. It was sequenced after W1/W5 on the argument that two
  cold shapes beat one plus a body read per item — which assumed body reads were cheap. They are
  not; nothing here is, because the cost is the round trip.
- **W3 halves the problem where W9 removes it.** Serving completed bodies over one HTTP request
  each took the open path from two requests per body to one. It is still one per body.
- **W1 is not the story.** A cold shape on the `seaweedfs-ovh` volume is sub-second at this size.
  It may still matter for a long conversation's first shape; nothing here measures that.
- **W5 is worth much less than derived.** Redefining the entity shape on every open costs one
  sub-second creation, not twenty seconds. The argument for it is now shape hygiene — a shape is a
  partition, and ours is a viewport — rather than latency.

## A body may now run ahead of the metadata that names it

`test_http_admission_ahead_of_replay_does_not_skip_earlier_events` fails on this branch, and it is
right to. Its gate holds `/sync/entities` while leaving `/sync/payload-chunks` alone, so the browser
holds an item whose `text_ref` still names an earlier revision while that field's later chunks are
already in Electric. It used to render the earlier revision. It now renders everything the chunk
stream has: `"Test retained prefix and preceding delta A and preceding delta B"` where the item's
own metadata says `"Test retained prefix"`.

Both halves of that were removed here, and neither removal was wrong alone:

- **W2** dropped the server-side `chunk_index < $8`, on the argument that `ActivePayloadBody`
  already filtered the prefix client-side. True when written.
- **W4** then deleted that client-side filter along with the `payload-interest` fetch that fed it
  `chunk_count`, on the argument that a contiguous run from index 0 is the whole value at _some_
  revision of the generation. Also true — and that is the problem. Some revision is not the one the
  conversation is showing.

So an item's text and its metadata can disagree, which the conversation model does not allow: a
revision is what makes a body and the row naming it one fact. The shape staying per generation is
still right; what is missing is a bound the reader can apply without a second request.

**The fix is the extent on the reference** — `chunk_count` and `content_bytes` on `PayloadRef`,
which this plan proposed as W4 and then abandoned for the contiguity rule precisely to avoid a
schema change. It is not an optimisation. It is what lets a reader render exactly the revision its
metadata names, from a shape that carries more. W9 needs it for the same reason and more sharply: a
window-scoped chunk shape delivers every item's chunks at once, so every body it feeds needs its
own bound. One epoch bump carries both.

## Measurement gates

None of the landed work is accepted on a passing build.
`agentplane/acceptance/test_conversation_latency.py` is the instrument for the first two: it opens
a real one-turn conversation through the deployed app's own routes and times the interest, the
entity snapshot and the bodies separately, cold and warm. Its ceilings are regression gates rather
than targets, and it has not yet been run against the deployment.

- Open-to-first-text for a Thread with a 30-segment tail, cold and warm, with the upstream
  shape-creation duration reported separately from transfer.
- Distinct Electric shape handles created during one turn of an agent run. The target for the
  landed work is zero new shapes per completing item; what remains attributes to the entity
  interest, which is what decides W5.
- Small-file create/fsync latency on `seaweedfs-ovh` versus `local-path-ovh-ssd` from the Electric
  pod's own node, so W1's rationale is a number rather than an inference from the storage class
  description.

## Deliberately out of scope

- Replacing Electric. The protocol usage is valid; the granularity was wrong for immutable
  one-shot payload bodies, which the landed work corrects.
- A longer-lived `Cache-Control` on payload bodies. `private, no-cache` with an `ETag` saves the
  transfer while revalidating authorization on every read; a real `max-age` would let a cached
  body outlive a logout, which the browser's HTTP cache gives no way to clear.
- The open acceptance gates in `agentplane/debug/conversation_acceptance.md` — long-run browser
  cache retention, container-wide Electric memory, the assembled-stack regression. Latency work
  does not close them and they do not block it.
