# Sampler journal falls behind provider streams

On 2026-11-23 the staging sampler was still processing Responses API events tens of seconds
after the provider reported completion. The delay was visible in the sampler journal before
History export or browser rendering. No change to the provider or UI is needed to reproduce it.

## How far behind?

From a read replica, take the most recent 3,000 journal entries per Session with a provider
attempt lease ending in the last 48 hours or later. Within the 48-hour window, select complete,
unfragmented `provider/received` records for `response.completed` with provider timestamps.
The scan excluded stored entries of 250 KB or larger. These are recent tails of active Sessions,
not all calls or a random sample; incomplete and especially long responses can be missed.

| Model | Sessions | Calls | Provider generation, median | Provider completion to journal, median | Completion to journal, p90 |
| ----- | -------: | ----: | --------------------------: | -------------------------------------: | -------------------------: |
| Astra |        4 |    18 |                        10 s |                                   47 s |                      328 s |
| Sol   |        2 |    10 |                         4 s |                                   82 s |                      230 s |

Generation is provider `completed_at - created_at`; the second interval ends at sampler
`Event.at` for the terminal event. These compare clocks and the provider timestamps have
one-second resolution. The Langfuse join on attributed traces and generations returned no
rows; it did not supply these measurements.

**Same clock, same bytes:** in one sampled Astra call, match complete SSE data lines in
`provider_wire` records to parsed delta records by attempt and provider sequence number.
For 102 matches, the median wire-record-to-parsed-record delay was 1.88 s, maximum 5.90 s.
Another call yielded 1.88 s across 83 matches. These bytes were already in the sampler;
network transit from OpenAI and browser paint cannot explain that interval.

## Where does the sampler spend time?

In the first 1,000 History events of one Session, use source sequence references to pair a
parsed provider boundary with its projected text or tool-argument delta. For the second
interval below, require consecutive journal entries and both provider events in the same
captured read. No new network read is needed between those events.

| Sampler timestamp interval              | Observations | Median |    p90 |
| --------------------------------------- | -----------: | -----: | -----: |
| Parsed boundary to projected delta      |          228 |  26 ms |  68 ms |
| Projected delta to next parsed boundary |          196 | 221 ms | 374 ms |

Separately, sample the primary's `pg_stat_activity` for the sampler database for 35 seconds,
with at least 10 ms between reads. Classify a transaction by observing the live-attempt existence
check (provider evidence) or attempt lease UPDATE (renewal). Track backend PID and transaction
start; on observing idle after COMMIT, measure from transaction start to COMMIT query start,
then from query start to state change.

| Observed transaction | Count | Before COMMIT, median / p90 | Server COMMIT, median / p90 |
| -------------------- | ----: | --------------------------: | --------------------------: |
| Provider evidence    |    27 |             515 ms / 991 ms |           0.47 ms / 0.70 ms |
| Lease renewal        |     9 |             414 ms / 668 ms |           0.47 ms / 0.48 ms |

Polling favors long transactions and can miss short statements or intervening transactions.
These are approximate samples across the database, not spans joined to the deltas above;
do not add the medians. Time before COMMIT includes statements, locks, round trips and
client work between statements. The COMMIT measurement excludes the return trip to the client.

Mimir shows a five-minute mean write time of 0.23 ms on the primary's block device around the
traced call (write time divided by writes completed). Over 48 hours, the median and p95 of
five-minute means were 0.22 and 2.83 ms. Sampler containers accumulated about 9 ms of CPU
quota throttling in total. Neither measure accounts for the observed delays; neither excludes
short I/O stalls, lock contention or time waiting to run.

## TabbyAPI reasoning chunks in staging

On 2026-11-23, the staging sampler at `f0f83ce` took a median **43.35 s** to project a
Qwen delta after recording its bytes. Both endpoints are sampler journal timestamps. The
[Chat Completions reader](../providers/chatcompletions/stream.go) emits a text delta for each
nonempty reasoning chunk during sampling and waits for evidence persistence and its progress hook before
consuming the next chunk.

| Projected deltas                              | Matches | Wire to projection, median |      p95 |  Maximum |
| --------------------------------------------- | ------: | -------------------------: | -------: | -------: |
| Sampled Qwen thread, recent tail (84 minutes) |   2,311 |                    43.35 s | 101.86 s | 135.23 s |
| Same thread, 49-second lock sample below      |      26 |                    69.21 s |  87.08 s |  91.46 s |
| Other matched streams in the lock sample      |      72 |                     5.88 s |  28.71 s |  30.55 s |

The replica scan took the latest 10,000 journal entries from the TabbyAPI Qwen thread
(`c7c5ab39…`) and 2,200 per other Session. It skipped entries of 30,000 stored bytes or
more and entries that expanded beyond 1 MB. Within each Session and attempt, match a complete SSE
data line in a wire record to a parsed chunk by canonical JSON, keeping only unique matches.
Follow the projected text or tool-argument delta's source sequence back to that chunk.
The short-window rows select by projection time, so the corresponding wire read can be earlier.

During those 49 seconds Qwen accounted for **102 of 577** counted journal entries across four
active Sessions. All 34 parsed Qwen chunks carried reasoning. This measures journal volume.
The effect on other conversations remains open.

Across six calls with a recorded HTTP response head, completion-token usage and `[DONE]`,
effective throughput was **1.27–2.92 completion tokens/s** from head to done. One call
reported 1,632 completion tokens in 1,285.5 s. Tabby's `created` value stayed constant
within each call. GPU generation rate and browser paint time remain unmeasured.

## Which transactions held up stream writers?

From 18:29:31.636 to 18:30:21.041 UTC on 2026-11-23, sample the staging primary's
`pg_stat_activity` and follow `pg_blocking_pids` to the root blocker. All 496 snapshots
showed sampler database connections waiting on tuple or transaction-ID locks (median 11,
range 7–14). For 923 observations of a writer waiting on the conversation-header lock,
920 had a visible root. In **806 of 920**, the root's current or last statement fetched
`sampler_content` payloads (686) or traversed the recursive `sampler_context` prefix (120).
The counts weight long waits and repeat a blocker when multiple writers queue behind it.
A last statement can remain visible while PostgreSQL waits for the client.

Identify transactions by backend PID and transaction start, then look for a statement specific
to the caller anywhere in that transaction. Two callers appeared at the root of header waits.

- **Interrupt polling.** There were 15 distinct root transactions identified by the pending
  interrupt query, including two distinct blocker/waiter pairs where the waiter subsequently
  checked the live provider attempt to append evidence. The [50 ms poll](../internal/residentruntime/session/turn.go)
  calls [PendingInterrupts](../internal/sessionstore/processes.go), which loads the conversation
  under the owner lock and discards it before reading the interrupt IDs.
- **History export.** There were 16 distinct root transactions identified by the export checkpoint
  upsert. The sampled revision locked the Session and loaded its conversation before advancing
  the cursor. [CommitCheckpoint](../internal/sessionstore/admission.go) now upserts the checkpoint
  directly. The foreign key enforces Session existence and `GREATEST` retains the furthest cursor.

Most root transactions had no caller-specific statement in the sample. These chains establish
contention on individual conversations. The snapshots have no join from a blocked backend
to a particular Qwen delta.

## Fix candidates and what would distinguish them

The [Responses reader](../providers/responses/provider.go) waits for
[evidence](../internal/sessionstore/provider_evidence.go) on each event and then
[lease renewal](../internal/sessionstore/provider_attempt.go) on semantic progress.
Renewal locks and [loads the conversation](../internal/sessionstore/store.go), including
native contexts, while evidence already uses a smaller owner header. A projected delta also
requires multiple journal inserts, cursor updates and notifications in one transaction.

- **Reduce work per event:** test a header-only renewal and minimal intent fence; compare
  short and long contexts. This keeps the durability boundary unchanged.
- **Reduce round trips:** combine evidence and renewal atomically, or renew only with a safe
  margin before expiry. Check that stale owners still cannot append, and semantic idle timeout
  still measures real progress.
- **Batch journal writes:** amortize appends, cursor updates and notifications over a bounded
  batch. Measure first-delta delay as well as throughput; preserve ordering, raw evidence and
  reconnect/replay semantics. Reading ahead alone could just move the backlog into memory.

**Interrupt delivery proposal for review.** A user interrupt already creates a durable
`sampler_interrupt` row. [Command admission](../internal/sessionstore/admission.go) also
notifies `central_sampler_work` in that transaction. The [replica listens](../cmd/main.go)
for work, but the in-flight turn still checks on its own timer. The direct
[RequestInterrupt path](../internal/sessionstore/processes.go) inserts the row without a wakeup.

Use the existing [keyed LISTEN/NOTIFY hub](../../postgres/wakeups/hub.go) to wake the affected
conversation on every interrupt insertion. Subscribe before the first pending-row read, then
re-read on wakeup and reconnect. Keep the generation and target-turn checks when acting on the
row. The ticker also checks for queued steering while a tool is in the foreground. Cover both
steering arrival and a tool entering the foreground with steering already queued before removing
the ticker. Test an interrupt committed during listener disconnection and during owner takeover.

Repeat the lock sample after the checkpoint change rolls out. To measure
cross-session interference, run the same Responses replay with export and interrupt handling
active, once alongside a Qwen reasoning stream and once alone.

Compare candidates with an identical burst of already-buffered SSE events at multiple context
lengths. Record per-operation wall time (including pool acquisition), transaction count, first
delta, wire-to-projection p50/p95 and terminal lag. Inject an ownership change mid-stream and
verify what the successor and History readers observe. Repeat a bounded staging comparison
before closing the bug: the deployment's database latency is part of this failure.
