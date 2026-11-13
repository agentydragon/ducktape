# Bridge shard distribution

At source `a69a053dc490f06139393129ff0309e0dbc5d1f9`, compare only
`//agentplane/app:test_bridge` in 4/8/8/4 order using
`--test_sharding_strategy=forced=4` or `forced=8`, with
`--nocache_test_results --flaky_test_attempts=1 --runs_per_test=1`.

| Shards | Invocation                                                                            | Bazel elapsed (s) | Physical test envelope (s) | Total worker CPU (s) |
| ------ | ------------------------------------------------------------------------------------- | ----------------: | -------------------------: | -------------------: |
| 4      | [d8c6286f](https://app.buildbuddy.io/invocation/d8c6286f-dee8-4df3-aff1-c97bf79f0ebd) |           152.384 |                    136.785 |              643.338 |
| 8      | [c3a8f9fa](https://app.buildbuddy.io/invocation/c3a8f9fa-9fe5-4d9a-b67d-a8b8b4466ca9) |           115.030 |                    100.710 |             1194.584 |
| 8      | [27c6cd15](https://app.buildbuddy.io/invocation/27c6cd15-dac4-4cca-b8b9-a30ee243b8e3) |            88.687 |                     76.343 |              727.694 |
| 4      | [5eba07f0](https://app.buildbuddy.io/invocation/5eba07f0-649c-497a-ae3f-11a867a3a466) |           118.862 |                    105.738 |              537.243 |

All 24 distinct physical TestRunner actions were new, attempt 1, and passed.
Each invocation ran the identical 24 cases with zero failures or skips; sorted
case-name SHA256 is `aa9356035cfba9ca4795c4305f6a422d365d00c41884106aa10369171c4d0720`.
Every runner completed cleanup. Per-action remote queue was about 0.06s.

Eight shards reduced mean selected-target elapsed by 24.9% and its physical
test envelope by 27.0%, using 62.8% more total worker CPU. It starts eight
PostgreSQL containers and overlaps eight workers instead of four. Per-action
peak memory remained about 1.2GB; summed per-action peaks are not a measured
simultaneous host peak. Both shard settings varied substantially across runs.

The architecture branch adopts eight as a scheduling candidate with unchanged
case assertions and fixture isolation. This is no matched complete-CI saving:
retained native targets near 90s can become the tail, broad ASGI changes select
other projects, and busy-window/four-PR capacity qualification remains. Keep it
on the architecture PR until that resource tradeoff is assessed; do not infer a
devel backport or a smaller required gate from this target-only probe.
