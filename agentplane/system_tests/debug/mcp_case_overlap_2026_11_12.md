# MCP Actions case-owned OAuth overlap

Giving each harness its own operator/server linkage removes a serial OAuth tail
while keeping all five cases in the existing two-slot batch. Two candidate runs
passed all cases and had shorter case envelopes than both pinned controls.
Their mean whole-action time and observed CPU consumption were higher, so this
is a case-DAG improvement with no established CI or compute saving.

## Ownership and coverage

Both OAuth server IDs use the existing real Dex-backed fixture endpoint. The
server ID scopes the linkage; the operator remains the ordinary test operator.
Each harness owns its server ID, browser login, app and Dispatcher clients,
Sandbox, policy objects, model scope and teardown. It still links through Dex,
executes the protected echo tool and asserts the receipt. No extra server Pod,
worker, fixture stack or case slot is created.

The launcher may hold its parent, launched child and unrelated Sandbox. A
concurrent BFF decision may hold two Sandboxes, giving a conservative bound of
five: exactly the fixture cap, with no spare capacity. The bound comes from
resource ownership, not a measured Pod-count time series. OAuth holds one
Sandbox per case. Existing policy cleanup shields deletion during cancellation;
model-request queues remain case scoped. No idle duration or product assertion
is shortened. All old callers and `mcp_servers.OAUTH` references were replaced
in the same change.

## Pinned physical comparison

Control source is literal `4415ac2544aef190cce5596d5ee2e73ba627b56f`.
Candidates add only the MCP ownership/batching change; their ordinary source
commit is `334ba7916c4c6f791d4afbeb4b744e84832992b6`. Each arm used:

```bash
bbr test //agentplane/system_tests/suites/mcp:test_actions --nocache_test_results
```

The first control also selected Pod, managed VM and four KVM runs. The other
arms selected MCP alone; the first candidate also ran fixture rendering. These
are overlapping, externally ordered A-B-B-A observations on multiple workers,
not a same-worker randomized experiment. The adjacent JSON preserves each
physical ID, Action digest, state, CPU time and case span.

| Arm | Invocation                                                                            | Baseline |    Setup | Case envelope | Whole process | Observed CPU |
| --- | ------------------------------------------------------------------------------------- | -------- | -------: | ------------: | ------------: | -----------: |
| A1  | [b2fe7d70](https://app.buildbuddy.io/invocation/b2fe7d70-e305-4bf1-a933-bca60f59405b) | warm     |  81.836s |      112.542s |      254.179s |     717.397s |
| B1  | [e49415a4](https://app.buildbuddy.io/invocation/e49415a4-192f-467f-93f8-bb5aa5d52248) | warm     |  88.424s |       90.744s |      233.668s |     692.380s |
| B2  | [dbc71c1c](https://app.buildbuddy.io/invocation/dbc71c1c-dbbb-40d4-b499-15a4071bcd76) | warm     | 133.814s |      102.923s |      298.064s |     819.374s |
| A2  | [c9eecce1](https://app.buildbuddy.io/invocation/c9eecce1-a550-458f-bed6-51ed96fc39fc) | cold     |  89.098s |      112.130s |      254.517s |     730.148s |

The two control Actions have the exact same digest, as do the two candidate
Actions. Their Commands and image-reference manifests match. A full CAS-tree
comparison between the first control and candidate identifies only the intended
four runtime files: suite entrypoint, MCP scenario, server IDs and rendered MCP
fixture. OCI images are byte-identical in these four arms.

All four JUnit files contain the same five passing cases with no skips, errors
or failures. Every arm certified complete cleanup and had distinct namespace,
PostgreSQL Pod and root Secret UIDs. The final cold control must not be treated
as a second warm observation. A1 and A2 share a worker identity; that does not
make the two intervening candidate executions worker matched.

Mean case envelope was 112.336 seconds control versus 96.833 candidate, a
15.503-second difference. The change lets OAuth run while the launcher is still
working, rather than after the earlier three-case batch has completely ended.
However, mean whole process was 254.348 versus 265.866 seconds, setup 85.467
versus 111.119 seconds and observed CPU 723.772 versus 755.877 seconds. Host load,
cache/state and overlapping work vary; these means are small-sample descriptive
results, not a causal latency or cost estimate.

A separate newer-source graph at `42e142053c685c792366bd47620d45fbe43513e8`
ran in [3b6a56ab](https://app.buildbuddy.io/invocation/3b6a56ab-9e98-4428-964c-4e9ef30b2ed3).
Shared `origin/devel` had advanced before that branch was created. CAS audit
found eight auxiliary image entries changed by the central sampler History
exporter extraction; that observation is explicitly excluded from this A-B-B-A
comparison. Pin literal source commits before follow-up comparisons.

## Required CI and next evidence gate

The source/capacity-comment head `d3a16a55ce03945ccb8bd3099ce023d48009d118`
passed all three required checks. Its actual required test invocation
[8fc46831](https://app.buildbuddy.io/invocation/8fc46831-49f2-5de7-b099-3d12bd3493bd)
contains a fresh, non-cached MCP Actions pass. The selected experiments are not
the complete required CI graph, and this note does not translate their case
reduction into required-gate feedback savings.

After adoption, inspect seven complete UTC days, or the first 50 fresh MCP
Actions executions if later. Exclude cache hits, preserve retries and missing
artifacts, stratify source/fixture fingerprint and cold/warm state, and compare
case envelope, setup, CPU, fixture scheduling errors and exact-head last-gate
completion. A persistent case reduction without whole-gate benefit is a cue to
work on the actual remaining bottleneck, not to raise case concurrency or shard
more. Tracking: #16029.
