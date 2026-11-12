# Staging PostgreSQL exhaustion and retained evidence

Follow-up: [sampler cutover and live storage measurements, 2026-11-12](../central_sampler/debug/2026_11_12_native_storage_cutover.md).

## Incident, 2026-11-11

At 09:53 UTC, History Service logged failed event pushes and stalled thread folds: PostgreSQL
could not extend relation files (`No space left on device`). By 09:58, Electric's logical
replication repeatedly failed because PostgreSQL could not write a `pg_logical/snapshots` file.
Electric exited, its Service had no ready endpoint, and the app's [Electric proxy](../app/go/proxy/electric.go)
returned `thread sync is unavailable`. At 10:02, CNPG reported `Not enough disk space` and
`Ready=False`; the primary container restarted with `no free disk space for WALs`. This is an
incident snapshot, not a claim about current status or recovery.

The primary's 16 GiB volume had zero available bytes; the replica's also reported 100% used.
The logical replication slot was inactive and retained about 9 MiB of WAL at the measurement,
so slot retention was not the main storage consumer. The largest databases were:

| Database          |     Total |
| ----------------- | --------: |
| `sandbox_service` | 6,899 MiB |
| `notifications`   | 4,225 MiB |
| `central_sampler` | 3,055 MiB |

The immediate recovery path is to restore disk headroom on both database instances, verify
PostgreSQL and Electric recover, then verify history export and the thread fold catch up. A volume
increase is capacity relief, not a representation fix. No recovery or deletion was performed in
this investigation. Existing evidence and user-authored data must not be discarded as a shortcut.

## Storage measurements

PostgreSQL relation totals include heap, indexes and TOAST, and are not additive with the payload
figures below. Sizes and counts were measured on the primary before it went down; the request-size
buckets were measured on the read replica afterward. They are point-in-time values.

| Relation                         |     Total | Notable components                               |
| -------------------------------- | --------: | ------------------------------------------------ |
| `sandbox_service.session_event`  | 4,937 MiB | 1,836 MiB heap, 780 MiB indexes, 2,321 MiB TOAST |
| `notifications.github_delivery`  | 3,646 MiB | 3,335 MiB TOAST, 140 MiB indexes                 |
| `central_sampler.sampler_event`  | 1,859 MiB | 1,366 MiB TOAST, 200 MiB indexes                 |
| `central_sampler.sampler_intent` | 1,175 MiB | 1,165 MiB TOAST                                  |

`session_event` contained 1,531 MiB of stored payload for native events (kind 20) and 1,463 MiB
for boundary records (kind 31). The largest sampler Session had 745 MiB of stored `sampler_event`
payload and 743 MiB in its exported `session_event` payload. These event totals include more than
model requests; they do not measure request duplication by themselves. Native-event growth remains
uncharacterized. [Delta settlement](../docs/adr_delta_settlement.md) removes only eligible deltas;
it does not imply that every retained event can be discarded or that deleted space has returned to
the filesystem.

GitHub deliveries occupied about 3.3 GiB of TOAST and arrived at roughly 0.3–0.5 GiB of
compressed payload per full day in the observed sample. Rows spanned November 4–11, consistent
with the documented [seven-day default retention](../notification_service/README.md#github-event-retention).
This is evidence of volume over a retention window, not quadratic growth. TOAST allocation includes
space beyond the summed live payload; the two figures are not interchangeable.

## Repeated model request snapshots

The sampler retains each full provider request before dispatch. Across 1,392 sampling intents,
`pg_column_size(request)` summed to 1,072 MiB. In the largest conversation, 471 requests summed
to 581 MiB. Grouping those requests by the number of messages in the request body showed:

| Messages in request | Requests | Mean stored request |
| ------------------: | -------: | ------------------: |
|                2–49 |       19 |              70 KiB |
|             450–498 |       19 |           1,128 KiB |
|             950–998 |       22 |           2,337 KiB |
|         1,000–1,049 |       24 |           2,462 KiB |

This is consistent with a roughly linear increase in each snapshot's size as that conversation's
context grows. Retaining every successive snapshot yields approximately quadratic cumulative
storage in the number of calls while context grows at a roughly constant rate. Context compaction,
nonuniform message sizes and calls per turn affect that scaling; the sample does not prove that
every conversation has the same behavior.

There is additional representation amplification:

1. [ProviderRequest](../central_sampler/types.go) contains both `body` and `wire_request`, derived
   from the same outgoing bytes in [provider_dispatch.go](../central_sampler/provider_dispatch.go).
   `body` is parsed JSON and `wire_request` retains the exact string. Physical savings from removing
   a field cannot be inferred by summing `pg_column_size` of extracted JSON fields because TOAST
   compresses the whole stored value.
2. [Begin](../central_sampler/store.go) stores the intent and records it as boundary evidence in
   the sampler's event journal. The exporter copies immutable events to History Service. The
   [sampler contract](../central_sampler/SPEC.md#evidence-and-continuation) intentionally retains
   transport evidence and uses the journal for continuation and recovery.

## Design questions

- Can parsed `body` be derived from one retained exact wire representation while preserving every
  reader and the pre-dispatch intent contract? Measure physical savings rather than assuming 50%.
- Can successive requests refer to immutable conversation records or use lossless deltas, while
  reconstructing the exact bytes sent, including ordering, request-specific parameters and
  compaction boundaries? If not, retain the exact bytes independently.
- Which event copies must remain independently available for sampler recovery, history replay,
  source links and audit? Any deduplication needs explicit ownership, failure and retention semantics;
  neither a History ACK nor a display projection alone proves the sampler copy is disposable.
- What are the event kinds and producers behind native-history growth, and what are the physical
  savings and read/write costs of any candidate representation? How should webhook retention and
  stored payload fidelity reflect actual consumer needs?
- Measure database growth and alert on filesystem headroom early enough to recover before writes
  and WAL fail. A ready pod or CNPG `Ready=True` did not guarantee headroom before this incident.

## Reproducing the measurements

Use read-only SQL against the appropriate database. `pg_column_size` on the stored column reports
its stored datum size; totals do not include table/index overhead. On a full primary, aggregations
that spill to temporary files can fail, and inspection itself can add load. Choose a suitable replica,
set a statement timeout and bounded `work_mem`, and avoid retrieving payload contents.

```sql
-- Run separately in sandbox_service, notifications and central_sampler.
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total
FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 12;

-- In central_sampler, substitute the conversation UUID being investigated.
SELECT (jsonb_array_length(request->'body'->'messages') / 50) * 50 AS message_bucket,
       count(*), pg_size_pretty(avg(pg_column_size(request))::bigint) AS mean_request
FROM sampler_intent
WHERE conversation_id = '<conversation UUID>' AND request->>'method' = 'POST'
  AND jsonb_typeof(request->'body'->'messages') = 'array'
GROUP BY 1 ORDER BY 1;
```
