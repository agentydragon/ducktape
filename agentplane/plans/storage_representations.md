# Storage representations, content sharing and compression

**Unscheduled design follow-up, requested 2026-11-12.** Reduce the physical cost of retaining
Agentplane activity, including the several representations of the same content in central sampler,
History and hosted harnesses. This plan records measured targets and design requirements; it does
not select a shared storage service or authorize a data reset.

Coordinate durable content ownership with the
[authored-conversation design](task_dag.md#agent_authored_conversations_design--conversations-agents-act-on-through-tools).
Keep harness-specific diagnostic activity separate from an optional authored conversation that an
agent communicates through. Storage efficiency must not require forcing divergent harnesses into
one user-visible transcript schema. Local improvements need not wait for that broader design.

## Measured targets

Read-only staging measurements on 2026-11-13, approximately 05:43–05:52 UTC, after removing old
sampler request snapshots and running `VACUUM FULL`. MB below means 1,000,000 bytes. Table sizes
include indexes and TOAST; payload figures count stored column values and exclude that overhead.
Uncompressed JSON repetition is not a prediction of physical savings. These are a dated baseline,
not current capacity figures.

| Representation                                |                                                                                  Measured cost | Opportunity                                                                                                                  |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------: | ---------------------------------------------------------------------------------------------------------------------------- |
| History ordinary events                       |                                                1,009 MB table; 760 MB payloads; 110 MB indexes | Share bodies and reduce small-event overhead                                                                                 |
| Sampler response evidence within those events | 76 MB raw transport payloads, 121 MB parsed stream-event payloads, 33 MB final sampling audits | Stop retaining the same response through each encoding; the audits' `events` arrays contain another 77 MB before compression |
| Sampler progress observations                 |                                                            63 MB payloads, plus row/index cost | Reduce or compress repeated operation identifiers and boundary/progress bookkeeping                                          |
| History projection chunks                     |                                    476 MB table: 296 MB text, 76 MB indexes, 54 MB key columns | Share content with source observations; improve granularity of small chunks                                                  |
| History event blocks                          |                      377 MB table; 314 MB gzip payloads retaining 2.95 million original events | Packing retains both native and semantic representations; compression alone does not remove those copies                     |
| Projection evidence and native links          |                                                      442 MB combined, including 209 MB indexes | All 1.43 million native-link rows repeat four keys already held in their evidence rows                                       |
| Projection manifests                          |                             180 MB for 733,134 revisions, including an 83 MB primary-key index | Reduce revision/key overhead while preserving immutable body references                                                      |
| Projection entities                           |                                           191 MB, including 87 MB JSON state and 72 MB indexes | Review repeated reference fields and necessary indexes                                                                       |
| GitHub webhook receipts                       |                                                 About 731 MB; 583 MB compressed payload values | About 145,000 deliveries repeat 3,980 distinct repository objects and seven sender objects                                   |
| GitHub normalized event/current state         |                                                                          About 247 MB combined | 131,243 current payloads equal their referenced event payloads, duplicating 57.8 MB of stored values                         |

Concrete content equality: a 1,629,022-byte tool output in Session
`787670a2-3b8f-4101-970b-e9f76fbe34b8` appears in native completion cursor 146413,
semantic completion cursor 146414 and projected output owner cursor 146406. Those values occupy
632,979, 526,797 and 632,335 stored bytes respectively: 1,792,111 bytes for three representations,
before row/index overhead. Another identical 455,262-byte output appears in 20 rows across five
Sessions. Distinct calls remain distinct observations even when their result bodies are equal.

Whole projection chunks of at least 1 KiB have only about 7 MB of repeated stored text internally.
Deduplicating just that table misses the larger duplication across representations. Its 511,780
text chunks include 449,883 smaller than 256 encoded bytes, so row/key/index costs matter too.
Manifests already share existing prefix chunks; summing their cumulative `content_bytes` incorrectly
counts those prefixes repeatedly.

Webhook repository objects account for 775 MB of uncompressed JSON text, including 754 MB of exact
repetition; sender objects add about 149 MB of repeated text. Exact complete receipt duplicates
account for only about 5 MB of stored payloads. Shared subobjects and retention are larger targets
than whole-receipt deduplication alone.

The [earlier investigation](../history_service/debug/2026_11_12_retained_request_amplification.md)
and [packing rollout](../history_service/debug/2026_11_12_event_packing_rollout.md) provide context;
their earlier capacity snapshots precede this baseline. The obsolete inline request snapshots are
already removed. Response logging and cross-representation body sharing remain open.

## Design work

### Remove redundant response capture

Trace [transport capture](../central_sampler/protocol/evidence.go),
[provider evidence](../central_sampler/provider_evidence.go), provider stream readers and the
[final intent audit](../central_sampler/store_intents.go). Decide which representation each
consumer actually needs. Prefer one retained stream with references from the completed operation
over another full `events` array. Keep final response content only where it supplies information
that cannot be referenced or derived from retained items.

Separate optional raw transport diagnostics from data required for recovery or acknowledged
history. Use existing typed configuration for capture/retention policy, considering per-Session
capture when needed. Preserve malformed/partial-response diagnosis and the existing rule that
failed evidence persistence stops processing. An unknown provider outcome must not trigger replay.

The bounded source probe found a narrower first change: successful sampling and compaction audits
copy `Response.Events` into both `sampler_intent.result` and result-phase boundary evidence. No
serving consumer was found that deserializes those successful audit arrays for continuation or
recovery. Compaction validates native output records; its audit is diagnostic, with Anthropic also
recording the replacement context. Recovery checks unresolved intents and rejection metadata.

The compact successful audit omits `events`, retaining the terminal response, usage and provider
HTTP metadata. Transient event collections remain for failure diagnostics, with per-event evidence
and atomic evidence/observation commits unchanged. Migrations 14 (sampler) and 17 (History) rewrite
existing successful results only after matching every duplicate event against its correlated
provider boundary. Fragment cursors, IDs/counts and source links are preserved. Stop sampler and
History writers/exporters for the coordinated rewrite, then vacuum the three affected tables.
After live verification, squash both migration chains and delete the rewrite implementation and
superseded compatibility paths. Raw wire capture remains a separate policy decision because the
SPEC promises received bytes, including incomplete SSE tails. The 33 MB result-payload group is
the measured target, not a guaranteed saving; the other response representations remain.

`FollowEvents` and History export expose the result boundary records unchanged, so the new summary
shape is an externally visible evidence-contract change even without a serving decoder for its
`events` field. Preserve the correlated provider boundaries and source links used by partial-item
reconciliation, and cover both export and direct event reads in the consumer checks.

### Share bodies across events and projections

Inventory the paths from native harness/provider observations to semantic completions,
[projection payloads](../history_service/internal/projection/payloads.go) and sampler continuation content.
Distinguish observation identity, body identity, derived display state and provider continuation
state. Store immutable body content once where ownership permits, with references from its
observations and views. Equal outputs from distinct calls share bytes without merging call identity.

Evaluate content addressing at useful boundaries: whole provider items/messages, content blocks,
message text and tool-output bodies. Keep tool-call arguments and signed reasoning blocks together
with their owning item when appropriate; do not introduce indirection for every scalar. Hashing,
serialization and compression remain private storage details. Reuse standard serializers and
existing provider-native types; avoid a generic JSON graph or a new universal harness schema.

Choose service ownership, authorization scope, lifetime and garbage collection before introducing
references across central sampler and History. A content hash grants no read authority. Deleting
one Session must not delete content retained by another, and references must commit atomically
with their content. Start with sharing inside an existing owner; a cross-service store is an open
decision, not a prerequisite for every local saving.

### Reduce metadata and improve compression

Review [event-block storage](../history_service/internal/storage/blocks.go), small projection chunks,
manifest revisions, evidence/native-link keys and entity indexes. Consider smaller internal keys,
coalescing tiny chunks, avoiding redundant reference rows, and packing additional eligible event
kinds. Preserve the distinct meanings of an observation, its source links and an immutable revision.

Benchmark the relevant alternatives with existing benchmark/profiling tools: PostgreSQL column
compression, current grouped gzip and another block codec where justified. Measure allocated
heap/TOAST/index bytes, WAL, append/read latency, CPU, maximum decoded allocation and partial reads.
Choose block boundaries and size limits from those results; do not compress an entire live Session
into one object or mistake better compression for eliminating duplicate bodies.

### Reduce webhook repetition

Review raw-receipt capture and retention separately from normalized observations. Consider dropping
unused envelope fields or sharing repository/sender subobjects where full receipt fidelity remains
useful. In [normalized commit storage](../notification_service/internal/store/github_commit_observations.go),
evaluate having current state reference its immutable event instead of copying its payload.
Account for event expiry: a current-state reference must retain its target or materialize the
required current value before that target expires. Subscription/inbox lifetimes remain independent.

## Correctness and acceptance

- Specify which interfaces promise original event bytes and which promise equivalent structured
  content. Preserve unknown fields, ordering, numeric values, opaque signatures and provider/model
  compatibility. Arbitrary body sharing must not reorder signed reasoning or change requests.
- Preserve Session cursors, watermarks, replay conflict detection, source links, reconnect/follow
  behavior and read pages beginning inside compressed blocks. Represent intentional removal with
  the existing removal/settlement protocol; never silently turn it into missing history.
- Verify raw and projected consumers, immutable payload revisions, diagnostic evidence reads,
  deletion/retention and concurrent append/read/GC behavior. Update contracts where the public
  [History API](../history_service/api/history/history_service.proto) is described when they change.
- Use bounded and resumable historical conversion where needed. Keep schema changes in new
  migrations and make any loss of retained data an explicit operator decision. Control backfill
  WAL/archive pressure and physical replica lag; logical sync health alone is insufficient.
- Demonstrate physical savings and acceptable read/write costs on representative reasoning,
  tool-output and webhook workloads. Verify live rollout and existing-data conversion separately;
  ordinary vacuum makes space reusable, while returning relation space to disk needs a separately
  planned rewrite or full vacuum.

## Suggested order

1. Review a concrete retention/reference design for sampler response evidence and remove redundant
   aggregate stream capture. This can land independently of the conversation redesign.
2. Prototype shared message/tool-output bodies across History's native, semantic and projected
   representations; measure savings and specify their owner and read/retention behavior.
3. Evaluate projection key/chunk/revision compaction and webhook storage independently, using the
   measured physical costs to choose the next changes.

Each selected change should be a wired, independently reviewable PR with focused correctness
evidence. Track implementation and rollout outcomes separately; remove completed work from this
plan and graduate surviving contracts beside their owning API/code.
