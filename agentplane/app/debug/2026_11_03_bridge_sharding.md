# Four shards for native bridge recovery cases

`//agentplane/app:test_bridge` covers 24 cases across Claude and Codex, with real
native processes, PostgreSQL and a session-scoped envtest Kubernetes control plane.
The recovery cases run serially within a shard. Four shards shorten that chain while
preserving fresh per-case service state and every existing assertion.

The experiment used source `3c5cfdefc46251957aaeca18495841b3831e3fff`, normal
`--config=ci`, `--nocache_test_results`, and `--test_sharding_strategy=forced=4` or
`forced=2`. The source also includes the bounded model waits and failure-artifact
changes from PR #14045; those do not change the successful cases' assertions.

| Run                                                                                                        | Shards | Slowest test action | Bazel elapsed |    Test CPU | Image imports        |
| ---------------------------------------------------------------------------------------------------------- | -----: | ------------------: | ------------: | ----------: | -------------------- |
| [First four-shard trial](https://app.buildbuddy.io/invocation/a047be85-657c-45b3-a884-7aee5b280ad9)        |      4 |              158.4s |        186.6s | 739.2 CPU-s | Three cold, one warm |
| [Four-shard repeat](https://app.buildbuddy.io/invocation/0ad1a271-4bc2-4a11-9613-565fb234acae)             |      4 |              135.7s |        138.6s | 535.7 CPU-s | One cold, three warm |
| [Same-source two-shard control](https://app.buildbuddy.io/invocation/e2d3d25a-85bd-4f8a-bc65-8e066af62642) |      2 |              202.4s |        206.6s | 431.9 CPU-s | One cold, one warm   |

Downloaded JUnit artifacts confirm exactly the same 24 unique passed cases in all
three runs, with no skips or duplicates. Four shards own six cases each; two own
12 each. The four-shard repeat and two-shard control each had one PostgreSQL/Ryuk
image import, while every shard started its own fresh PostgreSQL container. They
retained the same image family, per-action resources, native binaries and fixtures.

The matched-source repeat reduced the slowest action by 66.7s (33.0%) and whole
Bazel elapsed time by 68.0s (32.9%), at 24.0% more aggregate test CPU. These are
small-sample target measurements: workers and image warmth vary, and no fleet-wide
or all-required-check speedup is established. The cold four-shard trial also passed;
its extra imports raised total CPU and its slowest action to the values above.

The original two-shard PR run also finished its bridge actions in 214.4s and
157.4s, but its enclosing CI was held by a separate mypy execution without a result.
That run is not an end-to-end control for the shard change. If another test, build
or lint action finishes last, that action determines the PR feedback time.
