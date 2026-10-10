# `test_indexing` setup cost — 2026-10-10

`//agentplane/app/threads:test_indexing` spent most of its time projecting unrelated historical
items through the app projector in repeated 100-event batches. The test is about indexed reads and
a real old-item update after a large projected prefix, so its filler prefix now stays fully
archived in the history database and is projected with `fold.advance` plus the production
`ordered_entity_rows` serializer before bulk insertion. This retains the same item, payload,
evidence, entity-index, summary, epoch and checkpoint shape. The initial admission and `old-item`
events and the final `old-item` update still run through the History Service fold; the app no
longer has a projector of its own.

| Run                                                                                                        | Pytest time | Bazel test action |
| ---------------------------------------------------------------------------------------------------------- | ----------: | ----------------: |
| Before: [BuildBuddy invocation](https://app.buildbuddy.io/invocation/d522de09-0a2d-44b3-b442-2bf9a033777c) |    133.94 s |         147.325 s |
| After: [BuildBuddy invocation](https://app.buildbuddy.io/invocation/06737103-73f9-4e30-9ba6-830ed3c09387)  |     26.56 s |            41.3 s |

The after run passed all three parameter cases. Its pytest time was 80.2% lower and its Bazel test
action 72.0% lower in these single runs; this does not measure end-to-end CI feedback time. The
largest case retained 40,000 archived events and 20,000 materialized items. Traced peak allocation
was 660,404 / 593,644 / 602,362 bytes at 100 / 2,000 / 20,000 items, all below the unchanged
1,000,000-byte assertion. The 2,000- and 20,000-item query plans used `thread_entity_pkey` for
view-state, command, and touched-item lookups; PostgreSQL chose sequential scans for some lookups
at 100 items. The allocation peak increased from 512,354 to 593,644 bytes at 2,000 items and from
512,672 to 602,362 bytes at 20,000 items, so this change does not claim an allocation reduction.

A temporary cProfile run ([BuildBuddy invocation](https://app.buildbuddy.io/invocation/70f435e4-c4e3-4243-85e7-fee2a72f28c8))
ranked the history projector, fold, payload writes and SQLAlchemy execution/compilation as the
setup hot path. Its async cumulative timings overlap coroutine resumes, so they are useful for
ranking only. The temporary profiler code was removed.

The filler events are still folded by the pure production fold and remain readable from the full
History Service archive. This test no longer exercises the app projector's database persistence,
lease fencing, and notifications across the entire unrelated historical prefix. Those concerns
remain outside this indexed-access test. The existing large Bazel test size and all three history
sizes are unchanged.
