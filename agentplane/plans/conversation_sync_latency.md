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

The fix has to make the bound stable without unbounding the window:

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

**Commands** are a separate, much smaller case: that shape binds a sorted `entity_id IN (...)`
list, so every change to the selected set defines a new one. It is by-ID reconciliation of terminal
outcomes, which wants a lookup rather than a subscription — pending commands already stream on the
entity shape. The set changes at human pace, and the change sits on the recovery path four
lost-response cases cover, so it stays behind the entity work.

**What decides the order.** If a cold shape creation turns out to be milliseconds, the entity work
is not worth a schema change and W1 is the whole story; if it is seconds, this is the fix and it
does not depend on W1 landing. The measurement below answers that.

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
