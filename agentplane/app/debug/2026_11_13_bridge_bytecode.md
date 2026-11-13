# Bridge bytecode screen

Keep `pyc_collection` unchanged for now. The candidate is promising, but its cold
compile cost and this screen's cache changes prevent a complete-feedback claim.
This is separate from the existing eight-shard scheduling candidate.

The candidate adds only `pyc_collection = "include_pyc"` to `test_bridge` at source
`8a555b21141fded2166fcc17cd14096187e22842`. A/B/B/A runs use eight forced shards,
one run/attempt and uncached test results, with normal remote execution/caching.
They execute full test bodies, without import profiling or verbose import logging.
All four runs pass the same 24 canonical cases, with no failures, errors or skips:
32 NEW physical TestRunner actions, 96 case exposures and four correlated target
runs. They are not 96 independent reliability trials.

| Arm | Native invocation                      | Bazel elapsed | Critical path | Summed TestRunner CPU |
| --- | -------------------------------------- | ------------: | ------------: | --------------------: |
| A1  | `8c005b28-55fb-4788-8de6-c3ed66c36bf6` |       95.568s |        89.57s |              858.043s |
| B1  | `76d2f524-2e46-4ca6-9abb-c1084afac90b` |       85.256s |        81.62s |              684.271s |
| B2  | `b0c7bbd1-396d-4450-af30-8f5aea817dce` |       71.190s |        70.26s |              662.197s |
| A2  | `e5fea4c9-a342-4d0e-a0b9-743011246fa7` |       80.558s |        79.84s |              712.016s |

Mean selected-target elapsed is 88.063s A versus 78.223s B, down 9.840s (11.2%).
Mean critical path is down 8.765s. Mean summed physical TestRunner CPU is down
111.795s (14.2%); mean Bazel BuildMetrics CPU rises 9.115s. CPU sums overlap across
shards and are not wall time. The same eight reported TestRunner worker identities
appear in every arm; requested platform/resources and contemporaneous load are
unavailable in these records.

B1 reports 3,455 PyCompile actions: 3,452 cache hits and three NEW physical
executions using 3.845 CPU seconds. B2 has no physical compile executions.
A1 has 97 repository-fetch profile events and 364 completed actions; B1 has 3,471
completions, while warm B2/A2 have nine/twelve. These are different cache states.
Missing download-byte metadata remains unknown, not a measured zero.

The earlier one-shard collection-only screen at `a16af807` loaded 1,959 bytecode
objects and reduced mean summed import self-time 4.494s, with the same 24 names
and no bodies. Its cold B1 required 3,455 NEW compile actions and 4,202.992 CPU
seconds, adding 83.444s Bazel elapsed versus A1. That cold cost remains relevant;
the full-body run reused most compiled results and does not erase it.

Full normalized results, native profile identities, cache counters and denominator
limits are in [the curated evidence](2026_11_13_bridge_bytecode.json). Outer run:
`61cb80be-25d9-4378-996b-e2c8dcf8b24e`. The first collector attempt passed all eight
shards but then failed because minimal outputs had not materialized JUnit locally.
The repaired collector downloads the advertised XML and verifies shard identities
and the canonical case set before recording success. Raw BEP can contain
authentication headers; retain only allowlisted test-result fields in an archive.

Revisit alongside a qualified full candidate graph: compare cold/warm exposure,
total worker and Bazel CPU, and rerun-inclusive required clocks. Do not add more
isolated runs merely to improve this small screen's headline number. Neither
adoption nor a devel backport is justified by a complete-CI saving yet.
