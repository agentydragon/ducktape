# Sampler journal falls behind provider streams

On 2026-11-23 the staging sampler was processing Responses API events tens of seconds
after the provider reported completion. The delay was visible in the sampler journal before
History export or browser rendering. No change to the provider or UI is needed to reproduce it.

## After the two sampler changes

Both staging replicas were Ready on `3ce34c7` at 20:03 UTC on November 23. The image
includes [interrupt wakeups](https://github.com/agentydragon/ducktape/pull/19409) and
[checkpoint writes without the conversation lock](https://github.com/agentydragon/ducktape/pull/19412).
On the same Qwen thread, the median delay from recording bytes to projecting a delta fell
from **69.21 s to 8.55 s** in two 49-second windows. Most of the remaining delay accumulated
while the sampler processed earlier chunks from the same read.

| Staging observation                                   | Before, 18:29:32–18:30:21 UTC | After, 20:08:59.9–20:09:49.4 UTC |
| ----------------------------------------------------- | ----------------------------: | -------------------------------: |
| Sessions with counted journal entries                 |                             4 |                                3 |
| Counted entries, Qwen / all sessions                  |                     102 / 577 |                      602 / 1,582 |
| Parsed Qwen reasoning chunks / all parsed Qwen chunks |                       34 / 34 |                        198 / 198 |
| Qwen wire to projection, median / p95                 |             69.21 s / 87.08 s |                 8.55 s / 17.06 s |
| Matched Qwen deltas                                   |                            26 |                              156 |
| Other matched streams, median / p95                   |              5.88 s / 28.71 s |                  2.04 s / 4.36 s |
| Matched deltas from other streams                     |                            72 |                              203 |
| Snapshots with tuple or transaction-ID waiters        |                     496 / 496 |                          6 / 497 |
| Simultaneous waiters, median (range)                  |                     11 (7–14) |                          0 (0–1) |

Both windows use the replica journal matching rule below and 100 ms primary activity samples.
The repeat retained empty snapshots. The three active sessions were Qwen, Sol and Astra.
The workload and concurrency changed, so the comparison cannot assign the improvement to
either PR individually. Across the longer Qwen tails, the median / p95 went from
43.35 / 101.86 s (2,311 deltas in 84 minutes) to 6.24 / 14.36 s (1,474 deltas with both
endpoints between 20:03 and 20:10:27).

### Where the remaining time goes

In the repeat window, the 156 matched Qwen deltas came from **four wire reads**.
For each delta, measure the fraction of its wire-to-projection delay that elapsed after the
first matched projection from its read. The median fraction was **93%**. The reader was
already working through buffered chunks for most of that time.

| Qwen interval within the sampler                                           | Pairs | Median |     p95 |
| -------------------------------------------------------------------------- | ----: | -----: | ------: |
| Wire read to parsed chunk                                                  |   156 | 8.53 s | 16.98 s |
| Parsed chunk to its projected delta                                        |   156 |  18 ms |   77 ms |
| Projected delta to next parsed chunk, consecutive entries in the same read |   127 | 206 ms |  359 ms |

The third row measures time spent before the reader records another chunk whose bytes it
already has. The [Chat Completions reader](../providers/chatcompletions/stream.go) waits for
the [evidence write](../internal/sessionstore/provider_evidence.go) and the
[progress hook](../internal/providerio/timeouts.go) before consuming the next chunk.
In the measured image (`3ce34c7`), the hook renewed the lease on each reasoning delta.
Each renewal locked and loaded the conversation and fetched the intent before extending its lease.

At 20:11:30–20:12:05 UTC, the repeated 10 ms primary sample found a median **208 ms before
COMMIT for lease renewal** (15 transactions, p90 265 ms), and **48 ms for evidence** (21,
p90 88 ms). The corresponding baseline medians were 414 and 515 ms. Median server COMMIT
time remained below 0.5 ms for both. In the 49-second lock sample, 25 of 39 observations
of identified renewals showed the content fetch or recursive context query as the current
or last statement. These observations include client waits. They do not time either query
or link individual transactions to Qwen deltas.

The first change folds renewal into the evidence transaction. The owner, pending intent,
generation and live lease are checked there already. This removes a full conversation load
and a separate commit per semantic event. Replay the same buffered reasoning
chunks at the same concurrency and context length. Compare renewal duration, same-read
inter-chunk spacing and wire-to-projection p50/p95. If spacing remains high, measure the
evidence transaction's individual round trips before changing the write boundary.

### Tabby decode speed and terminal lag

For three complete post-rollout calls on this thread, match each sampler attempt to a Tabby
request by completion-token count and provider finish time within the sampler head-to-DONE
interval. Tabby's decode rate excludes prompt processing. The sampler rate divides the same
completion-token count by time from its recorded HTTP response head to its parsed `[DONE]`.

| Completion tokens | Tabby decode | Sampler head to DONE | Sampler effective rate |
| ----------------: | -----------: | -------------------: | ---------------------: |
|               220 |    177.4 t/s |                9.7 s |              22.57 t/s |
|             2,452 |    116.1 t/s |              186.6 s |              13.14 t/s |
|             4,384 |    108.5 t/s |              380.6 s |              11.52 t/s |

The six baseline sampler calls ranged from 1.27 to 2.92 t/s. On the 1,632-token baseline
call, Tabby logged 109.5 t/s and the sampler recorded 1.27 t/s. One 2,566-token call
entirely between rollouts recorded 3.06 t/s. On the 4,384-token post-rollout call, Tabby
logged completion at 20:07:27 and the sampler recorded DONE at 20:13:07, **340 s later**.
Wire-to-projection starts only when the sampler records a read. Bytes waiting upstream of
that read can add substantial delay. Browser paint and History delivery remain unmeasured.

Repeating the 3,000-entry Responses terminal scan for calls started after 20:03 yielded
one Astra completion (61 s from provider completion to journal) and five Sol completions
(median 36 s, range 20–78 s). The baseline medians were 47 s for Astra and 82 s for Sol.
These small post-rollout samples still show terminal lag in the tens of seconds.

## How far behind was the sampler before the changes?

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
within each call. The initial journal scan measured neither GPU decode rate nor browser paint time.

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

The [Responses reader](../providers/responses/provider.go) waits for each
[evidence transaction](../internal/sessionstore/provider_evidence.go). A projected delta still
requires journal inserts, cursor updates and notifications. Compare short and long contexts to
check how much each benefits from removing the separate renewal transaction.

- **Reduce lease writes:** coalesce deadline updates while enforcing ownership and expiry on
  every append. Check the idle budget after a last delta followed by silence.
- **Batch journal writes:** amortize appends, cursor updates and notifications over a bounded
  batch. Measure first-delta delay as well as throughput; preserve ordering, raw evidence and
  reconnect/replay semantics. Reading ahead alone could just move the backlog into memory.

**Interrupt delivery after the staging sample.** The turn now subscribes to its conversation
before reading pending interrupts. [Database triggers](../migrations/schema/0020_turn_wakeups.sql)
notify that key when an interrupt or command commits, including writes from older replicas.
The [turn](../internal/residentruntime/session/turn.go) re-reads durable state when the key
wakes or the listener reconnects. A tool entering the foreground also wakes the turn so that
previously queued steering input can yield the observation.

To measure cross-session interference, run the same Responses replay with export and interrupt handling
active, once alongside a Qwen reasoning stream and once alone.

Compare candidates with an identical burst of already-buffered SSE events at multiple context
lengths. Record per-operation wall time (including pool acquisition), transaction count, first
delta, wire-to-projection p50/p95 and terminal lag. Inject an ownership change mid-stream and
verify what the successor and History readers observe. Repeat a bounded staging comparison
before closing the bug: the deployment's database latency is part of this failure.

## Reusing a prepared conversation, 2026-11-23

The buffered Qwen replay compared separate context preparation, dispatch and settlement
against reusing the decoded native contexts for one fresh attempt. Both runs used three
trials of three concurrent requests, each with 96 retained messages (498 KB) and 24
reasoning deltas delivered in one verified wire read. The sampler journal supplied the
timestamps. Times below are pooled median / p95. With nine calls the p95 is the maximum.

| Interval                                                              | [Separate loads](https://app.buildbuddy.io/invocation/499dbfb6-4958-421d-ae51-b03faf711214) | [Reused snapshot](https://app.buildbuddy.io/invocation/774a95ea-addd-4460-a01b-5641e3dc7d60) |
| --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------: | -------------------------------------------------------------------------------------------: |
| Turn started to sampling dispatch                                     |                                                                             7.104 / 7.666 s |                                                                              3.688 / 4.024 s |
| Turn started to HTTP response head                                    |                                                                             7.685 / 7.937 s |                                                                              4.022 / 4.238 s |
| Parsed DONE to sampling result                                        |                                                                                429 / 487 ms |                                                                                   34 / 47 ms |
| Parsed DONE to completed turn                                         |                                                                             4.921 / 5.298 s |                                                                              2.933 / 3.451 s |
| Previous projection to next parsed chunk in the same read (207 pairs) |                                                                                  22 / 63 ms |                                                                                   15 / 46 ms |
| Wire read to projection (216 deltas)                                  |                                                                              583 / 1,415 ms |                                                                               312 / 1,026 ms |

From admission through runtime shutdown, PostgreSQL counted 323 recursive context reads
and 314 content fetches on the baseline, and 288 and 279 with snapshot reuse. These totals
include scheduling and turn completion. The hot path test also makes a transcript-only
blob unreadable after the driver's initial read and verifies dispatch and settlement still succeed.

BuildBuddy ran the revisions on separate workers. The parsed-to-projected median also
shifted from 13 to 8 ms, so the timing changes include run-to-run variation. Dispatch and
the work between sampling result and turn completion still take seconds on this fixture.
Repeat these boundaries on a long staging Qwen call after rollout.
