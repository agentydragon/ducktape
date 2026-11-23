# Responses stream falls behind generation

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

Compare candidates with an identical burst of already-buffered SSE events at multiple context
lengths. Record per-operation wall time (including pool acquisition), transaction count, first
delta, wire-to-projection p50/p95 and terminal lag. Inject an ownership change mid-stream and
verify what the successor and History readers observe. Repeat a bounded staging comparison
before closing the bug: the deployment's database latency is part of this failure.
