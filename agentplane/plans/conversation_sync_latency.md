# Conversation sync latency

Opening a Thread on staging takes tens of seconds before any conversation text paints,
and text that has already arrived over the wire renders much later than its response.
A HAR shows several requests spending ~20 s upstream while their bodies transfer in
milliseconds, so the cost is server-side shape work, not transfer or React.

This plan burns down once the work items land and the measurement gates pass.

## What the code does today

Three independent mechanisms stack on the open path, each adding a serialized stage:

1. `GET /threads/{id}/sync/interest` — app reads the projection checkpoint and the
   30th-last segment cursor (`agentplane/app/trajectory.py`, `conversation_entity_interest`).
2. `GET /threads/{id}/sync/entities` — Electric shape over `cursor >= tail_from`.
3. `ConversationCollection` renders nothing until the `view_state` row's `revision_cursor`
   reaches `through_cursor` (`onRows(caughtUp ? rows : [])`), so no body mounts before
   stage 2 completes.
4. For each mounted body, `GET /threads/{id}/sync/payload-interest` — app reads the
   manifest for `chunk_count`/`content_bytes`.
5. For each mounted body, `GET /threads/{id}/sync/payload-chunks` — a second Electric shape.

A tail of 30 segments therefore costs one entity shape plus up to 30 payload shapes,
behind four round trips that cannot overlap.

### Shape identity fragments per revision

An Electric shape is identified by its definition, `where` and bound parameters included.
`ElectricProxy.payload_chunks` appends `AND chunk_index < $8` with `$8` set to the selected
revision's `chunk_count` whenever `follow` is false, so **every revision of a payload field
is a distinct shape**, while the `follow` variant of the same field is a third. The bound is
redundant: `ActivePayloadBody` already filters `chunkIndex < expected` client-side before
assembling.

The consequence is visible in the reported symptom. While an item streams,
`projected_session.tsx` passes `follow={live && completion === null}`, so the body rides the
unbounded shape. The moment completion lands, `follow` flips false and the reference's
`revision_cursor` advances: the component drops that collection, fetches a new
`payload-interest`, and opens a **freshly defined** shape. The text was already in a delivered
response; the UI discards it and pays a cold shape creation to get the same bytes back.

The entity and command shapes fragment for the same reason, less sharply:

- `entities` binds `tail_from`, which the interest resolver recomputes on every mount and on
  every rotation (`segmentCount > 60`), so an actively growing Thread mints a shape per rotation.
- `commands` binds a sorted `entity_id IN (...)` list, so every change to the selected command
  set is a new shape. This is by-ID reconciliation of settled outcomes — a lookup, not a
  subscription. Pending commands already arrive on the entity shape.

`ELECTRIC_MAX_SHAPES=1024` runs an LRU cleaner once a minute, so this churn also pays
deletion work on the same volume that creations contend for.

### The render gate is a second round trip

`PayloadBody` gates output on `complete`: the assembled body's UTF-8 length must equal the
`content_bytes` reported by a **separate** `payload-interest` fetch. The effect during
streaming is that arrived chunks stay invisible until an independent HTTP response confirms
their extent, and `referenceKey` includes `revision_cursor`, so that fetch repeats for every
delta of every visible streaming body. This is an independent cause of "the text was in the
response but rendered later" and survives any fix to shape creation.

### Shape storage is on distributed HDD

`cluster/cdk8s/agentplane/electric.py` sets `ELECTRIC_STORAGE=fast_file` with
`ELECTRIC_STORAGE_DIR` on a 5 GiB `seaweedfs-ovh` PVC. That class is
"SeaweedFS CSI (RWX, distributed), media-blind (HDD bulk)" — a FUSE mount over network
storage. Shape creation writes a per-shape directory of small files; shape expiry deletes
them. Small-file create/fsync latency over that path is the most plausible mechanism for a
per-shape cost measured in tens of seconds, and unlike the churn above it taxes _every_
shape creation.

Electric's shape storage is a rebuildable cache: the acceptance matrix already records a
forced slot loss recovering automatically with old handles answering 409/must-refetch
(`agentplane/debug/conversation_acceptance.md`). It does not need distributed durability.

## Work items

Independently reviewable; only W3 depends on another item landing first. W1 is expected to
dominate, so land and measure it before spending review on W5.

### W1 — move Electric shape storage to node-local SSD

Change the PVC in `cluster/cdk8s/agentplane/electric.py` from `seaweedfs-ovh` to
`local-path-ovh-ssd` (NVMe, zone `hil-ovh`, `WaitForFirstConsumer`). The Deployment is
already `replicas=1` with `strategy=recreate()` and `attract_to_zone`, so nothing else moves.

Cost: a node drain loses the shape cache, and clients take one 409/must-refetch round.
State that in the chart comment as the accepted trade. Recreating the PVC discards existing
shapes, which is the same event.

### W2 — drop the redundant chunk bound

Remove `AND chunk_index < $8` from `ElectricProxy.payload_chunks`. Pinned and following reads
of one `(owner, field, generation)` then share a single shape, and a completing item stops
minting one. `payload_selection` still validates the revision and still supplies the extent;
the client filter that already exists does the bounding.

One line plus its test. Land it even though W3 subsumes it for completed bodies — it also
fixes the streaming-to-completed transition for bodies that stay on the shape path.

### W3 — serve completed bodies over bounded HTTP, not Electric

Add `GET /threads/{id}/conversation/payloads` taking up to N payload references and returning
each one's whole assembled body for its exact revision, or its typed unavailability. Immutable
references, so the response is safely `private` cacheable with a long max-age.

`projected_session.tsx` passes `follow=false` for every completed body, which is most of an
opened tail. Routing those through one batched fetch collapses stages 4 and 5 from ~60
requests and ~30 shape creations to one request and none. Streaming bodies keep the `follow`
shape, which is what Electric is for.

Bound the reference _count_, not the response bytes: the sync contract explicitly refuses
query byte budgets and truncated bodies.

### W4 — carry the manifest extent on `PayloadRef`

Add `chunk_count` and `content_bytes` to `PayloadRef`
(`agentplane/app/conversation_projection.py`). The projector writes the manifest and the
entity row in one transaction, so the extent can travel with the reference that names the
revision, atomically, on the entity shape the browser already holds.

`/threads/{id}/sync/payload-interest` and every fetch in `PayloadBody` then delete: the
render gate reads the extent it already has. This removes the per-delta round trip and the
window where arrived chunks render as "Loading complete revision…".

This changes the projected row shape. Per repo policy there is no production tier, so bump
the projection epoch and let the existing 410/rotation path recreate; write no migration and
no tolerant reader.

Depends on W3 only for review order — W3's endpoint should take the same enriched reference.

### W5 — entity and command shape churn (gated on W1's measurement)

If W1 makes shape creation cheap, rotation churn may not be worth addressing. If it is:

- **Entities**: quantize the shape's lower bound (`tail_from` rounded down to a granularity
  `G`) so successive interests reuse one shape and cross a boundary only every `G` cursors,
  over-selecting at most `G` cursors of metadata. `G` needs tuning against observed cursor
  density; this trades metadata rows for shape reuse and needs a measurement, not a guess.
- **Commands**: serve `GET /sync/commands` as a plain by-ID lookup over HTTP instead of a
  shape. It reconciles settled outcomes for a known ID set — the contract's "Get commands by
  ID", a lookup. Pending commands already stream on the entity shape.

### W6 — debug observations page

`chronological_debug.tsx` requests 30 observations with full `entry` payloads and renders
nothing until the whole page parses, even though `JsonView` is already gated on expansion.
Split the endpoint: list `cursor`, `kind`, `source_id`, `source_sequence` and the entry's
size, and fetch one `entry` when its `<details>` opens. This is independent of Electric.

### W7 — telemetry for shape creation

We are inferring Electric-side cost from HAR timings. Before and after each item above:

- Log upstream duration and the returned `electric-handle` in `ElectricProxy._forward`, so a
  cold creation is distinguishable from a warm snapshot in app logs.
- Enable the pinned image's supported telemetry export (check `electricsql/electric:1.8.1`
  for its Prometheus/OTLP env vars) and point it at the cluster collector.

Land this first if W1 cannot be scheduled immediately; it is what turns the next report into
evidence.

### W8 — reconsider the blanket `no-store`

`_forward` overwrites Electric's cache directives with `cache-control: private, no-store`,
discarding the HTTP caching the protocol is designed around. Historical log segments at a
fixed offset are immutable. Rewriting `public` to `private` and preserving Electric's own
`max-age` would make reloads free without exposing a response to a shared cache.

The trade is that a browser's private cache outlives a logout, so this needs a bounded
max-age rather than `immutable`, and it does not help a cold shape. Lowest priority; decide
after W1.

## Measurement gates

None of the above is accepted on a passing build.

- Record the open-to-first-text time for a Thread with a 30-segment tail, cold and warm,
  before and after W1. Report the upstream shape-creation duration from W7's logs separately
  from transfer.
- Count distinct Electric shape handles created during one turn of an agent run, before and
  after W2 and W3. The target is zero new shapes per completing item.
- For W4, confirm streamed text paints without an intervening HTTP response: the entity row
  carrying the new revision and the chunks that satisfy it are the only inputs.
- Small-file create/fsync latency on `seaweedfs-ovh` versus `local-path-ovh-ssd` from the
  Electric pod's own node, so W1's rationale is a number and not an inference from the
  storage class description.

## Deliberately out of scope

- Replacing Electric. The protocol usage is valid; the granularity is wrong for immutable
  one-shot payload bodies, which W2 and W3 correct.
- The open acceptance gates in `agentplane/debug/conversation_acceptance.md` — long-run
  browser cache retention, container-wide Electric memory, the assembled-stack regression.
  Latency work does not close them and they do not block it.
