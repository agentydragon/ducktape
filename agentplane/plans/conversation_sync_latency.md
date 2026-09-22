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

### W5 — entity and command shape churn

Both remaining shapes still fragment, and both are now a much smaller share than when this plan
was written:

- **Entities.** The shape binds `tail_from`, recomputed on every mount and on every rotation
  (`segmentCount > 60`), so an actively growing Thread mints a shape per rotation and a reload
  during a run mints another. Quantizing the lower bound trades shape reuse against
  over-selection, and the naive form (round down to a multiple of `G`) admits an unbounded
  over-selection where cursors are dense, which would break the bounded-interest contract. The
  scheme that does not is page-aligned bounds over a stored per-segment index, since segment
  positions are stable under append — a schema addition.
- **Commands.** The shape binds a sorted `entity_id IN (...)` list, so every change to the
  selected set is a new shape. This is by-ID reconciliation of terminal outcomes, which wants a
  lookup rather than a subscription; pending commands already stream on the entity shape. The
  change is small but sits on the command-recovery path that four lost-response/reconnect cases
  cover, for the smallest of the remaining wins.

**Both stay open deliberately.** This item's gate was W1's measurement, and W1 is deferred, so the
data that would say whether either is worth its risk does not exist yet. The shape-creation log
above is what supplies it: it reports a handle per request, so counting distinct handles over a
run answers directly how much rotation and command churn actually cost.

## Measurement gates

None of the landed work is accepted on a passing build.

- Open-to-first-text for a Thread with a 30-segment tail, cold and warm, with the upstream
  shape-creation duration reported separately from transfer.
- Distinct Electric shape handles created during one turn of an agent run. The target for the
  landed work is zero new shapes per completing item; what remains attributes to rotation and
  commands, which is what decides W5.
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
