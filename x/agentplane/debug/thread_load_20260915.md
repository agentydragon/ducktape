# Short Thread replay inspection — 2026-09-15

Read-only inspection of staging Thread `70bf54a7-81e1-46f5-8fed-381ae1ce870f`
(`test-yde8x`, Codex), following the operator's report of visible multi-second loading.
The app image was `devel-20260915230405-dd0439f`. No input was submitted, harness
resumed, user credentials exported, or stored history changed.

## Response shape

The PostgreSQL archive contained 3,091 EventEntries for eight completed turns:
eight confirmed user messages, eight assistant-text items, 22 reasoning items, and
21 tool calls. Aggregate sizes below are `octet_length(payload::text)`, not compressed
HTTP transfer sizes. Message/tool contents were not printed.

| Kind               | Entries | JSON bytes |
| ------------------ | ------: | ---------: |
| Native frames      |   1,595 |  1,073,446 |
| Text deltas        |   1,310 |    364,032 |
| Completed items    |      51 |    116,460 |
| Tool-output deltas |      28 |     98,555 |
| All entries        |   3,091 |  1,694,643 |

The text deltas contain only 6,275 bytes of actual text. Native traffic repeats them
as 1,286 `item/agentMessage/delta` and 24 `item/reasoning/summaryTextDelta` frames.
Completed text/reasoning contains 6,278 bytes; completed tool outputs contain 87,434
bytes. One completed tool Event occupies 77,397 JSON bytes, with a corresponding
91,487-byte native completion frame. Small visible conversation size therefore does
not imply a small replay stream.

## Download observations

Authenticated GETs used the documented short-lived `agentplane-agent` ServiceAccount
token path, audience `agentplane`; credentials stayed in pipes and response bodies
were discarded. Standard curl timing, from the developer host:

| Request                     | Download bytes | First byte | Complete |
| --------------------------- | -------------: | ---------: | -------: |
| `/threads/ID/events/stream` |      1,790,008 |    0.102 s |  2.275 s |
| Same full SSE, repeat       |      1,790,008 |    0.102 s |  2.363 s |
| `/threads/ID/events`        |      1,651,350 |    1.524 s |  1.665 s |
| SSE with `after=2800`       |        142,075 |    0.101 s |  0.310 s |

These are a few host-to-app samples, not browser paint measurements or attribution
of the user's exact delay. Read-only `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)` on
`SELECT payload FROM event WHERE thread_id = ID AND cursor > 0 ORDER BY cursor LIMIT 10000`
returned 3,091 rows in 3.476 ms, all shared-buffer hits. That excludes client transfer,
payload decoding, and app serialization; it does not isolate those remaining costs.

Deployed source converts stored proto-JSON to protobuf and back before emitting one
SSE frame per Event. The client copies its accumulated Event array and publishes
subscribers on every arrival, then reconstructs timeline blocks. A browser/CPU profile
is still needed to quantify that additional work; multi-second HTTP completion alone
already rules out a browser-state-library-only fix.

The design consequence is tracked in the
[layering doc](../docs/thread_layering.md#planned-conversation-view-synchronization),
not specified independently here.
