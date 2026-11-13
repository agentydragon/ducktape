# Balanced Pod partition experiment

Two manual Pod targets passed the same 21 scenarios as the required Pod target on
independent fresh stacks. In one uncached, selected Pod + MCP Actions + VM graph
pair, the split Pod branch finished 98.744 seconds earlier relative to Bazel
start. The full selected graph finished 54.414 seconds earlier, but VM was last
in both runs and independently varied by 41.599 seconds. This pair does not
establish a causal whole-CI saving or the four-minute feedback objective.
Required CI still runs the original, unchanged 21-case `test_pod`; these targets
are tagged `manual`. This follows the [single TLS-tail experiment](pod_split_controlled_experiment.md),
whose extra fixture did not yield a demonstrated saving.

## Partition and invariant

The control's earlier [case spans](https://app.buildbuddy.io/invocation/1f32be52-d51a-44ff-b5e5-ab83a579cc92)
put 211.9 summed seconds in lane A and 228.3 in lane B. A selects storage,
idle Pod resume, agent messaging, model proxy, and Pod mTLS; B selects the other
16 cases. The two-slot `CaseBatch` keeps the original declaration order in each
lane. Storage runs exclusively before A's other cases, and each target creates
and tears down its own fixture stack. The idle-resume case retains its required
60-second idle wait; the split overlaps that wait with other case work rather
than shortening it.

The Pod runner pool retains each shared harness pair until stack teardown. A
creates at most one such pair before its later open case. B has one early shared
case; its later adjacent shared security/thread cases come after its last open
case. Thus neither lane can retain two pairs before a two-Sandbox open case,
which would exceed the fixture's five-Sandbox capacity. Both keep the existing
two-case concurrency limit, fresh Sessions, assertions, and case-owned cleanup.
There is no changed timeout, retry, or skipped case.

## Protocol and tested inputs

The local code was `5354b836af88ee17fbabc65c20666f90a0e6216c` plus the
two-file manual-target patch. Collection ran before physical execution:

```sh
bbr test //agentplane/system_tests/suites:test_pod_balanced_a_spike \
  //agentplane/system_tests/suites:test_pod_balanced_b_spike \
  --test_arg=--collect-only --test_arg=-q --test_output=all --nocache_test_results
bbr test //agentplane/system_tests/suites:test_pod \
  --test_arg=--collect-only --test_arg=-q --test_output=all --nocache_test_results
```

[Partition collection](https://app.buildbuddy.io/invocation/e9a0c08e-411f-49a8-a205-9e6b8b8388cb)
selected 5 and 16 pytest node IDs; [original collection](https://app.buildbuddy.io/invocation/1096f224-98a0-49c1-963f-e5de1e415c29)
selected 21. The node-ID multisets are equal and disjoint across lanes.
Collection exercised no fixture. The physical commands, run sequentially with
`--nocache_test_results --keep_going --test_output=errors`, were:

```sh
bbr test //agentplane/system_tests/suites:test_pod \
  //agentplane/system_tests/suites/mcp:test_actions \
  //agentplane/system_tests/suites:test_vm \
  --nocache_test_results --keep_going --test_output=errors
bbr test //agentplane/system_tests/suites:test_pod_balanced_a_spike \
  //agentplane/system_tests/suites:test_pod_balanced_b_spike \
  //agentplane/system_tests/suites/mcp:test_actions \
  //agentplane/system_tests/suites:test_vm \
  --nocache_test_results --keep_going --test_output=errors
```

Both full Bazel invocations completed successfully. Their original TestRunner
actions were new, `doNotCache=true` executions, not reused target summaries.
The decoded Pod actions request the same 4 compute units, 50 GB, Firecracker
image, persistence permission, and recycling key. Comparing the control and
each partition's target-normalized CAS runfiles trees found 123 identical common
subtrees and no differing common-path file. Both target-specific image manifests
have identical content digests. Command and input-root digests differ as
expected for target names, launchers, and pytest selection. The unchanged MCP
Actions and VM test actions have identical action digests between graphs.
BuildBuddy's runner fetched different `devel` bases and applied patches, so its
checkout SHA alone is not the tested-input identity. The
[machine-readable evidence](pod_balanced_split_experiment.results.json) retains
action IDs, JUnit names, timestamps, and fixture identities.

## Physical results

All seven JUnit results passed with zero failures, errors, or skips. Pod's 21
control cases equal the disjoint A five plus B 16 case names in the physical
JUnit outputs. Every fixture certified cleanup complete. All three Pod
fixtures were warm, and their namespace, PostgreSQL Pod, root certificate,
root Secret, and collector namespace UIDs are pairwise distinct. MCP Actions
was warm in both graphs; VM was cold in both.

| Graph / action          |  Queue | Worker prep | Execution |    Setup | Case envelope | Cleanup |       CPU |
| ----------------------- | -----: | ----------: | --------: | -------: | ------------: | ------: | --------: |
| Control Pod             | 0.060s |      0.428s |  422.720s | 105.173s |      248.625s | 33.464s | 1224.900s |
| Split A                 | 0.063s |      0.318s |  254.447s |  78.392s |      121.106s | 28.614s |  700.222s |
| Split B                 | 0.061s |      0.352s |  329.154s | 123.632s |      133.211s | 31.092s | 1016.192s |
| Control MCP Actions     | 0.061s |      0.503s |  262.897s |  92.830s |      113.592s | 29.300s |  749.109s |
| Split-graph MCP Actions | 0.061s |      0.394s |  326.997s | 130.256s |      130.768s | 29.885s |  913.512s |
| Control VM              | 0.066s |      7.823s |  501.375s | 219.927s |      195.548s | 31.422s | 2034.317s |
| Split-graph VM          | 0.066s |      0.321s |  459.776s | 181.057s |      205.109s | 30.170s | 1925.319s |

The Pod case envelope fell from 248.625 seconds to the slower lane's 133.211
seconds in this run. The split duplicated setup (202.024 total versus 105.173
seconds) and cleanup (59.706 versus 33.464 seconds). Its two physical action
spans sum to 583.601 seconds versus the control's 422.720. At the decoded
4-CU request, this is 38.907 versus 28.181 **requested execution CU-minutes**,
an increase of 10.725. Observed CPU rose from 1224.900 to 1716.414 seconds.
These are allocation and observed CPU measures, not BuildBuddy billing.

| Selected graph                                                                       | Bazel elapsed | First TestRunner queued from Bazel start | Last Pod worker done | Last TestRunner worker done | Limiting action |
| ------------------------------------------------------------------------------------ | ------------: | ---------------------------------------: | -------------------: | --------------------------: | --------------- |
| [Control](https://app.buildbuddy.io/invocation/0c4b1ad2-fed1-4ee0-97db-040151a659e0) |      548.370s |                                  38.252s |             462.175s |                    548.170s | VM              |
| [Split](https://app.buildbuddy.io/invocation/c0bd6ca1-f87a-41ff-ac9d-3f5838dda2e7)   |      493.956s |                                  32.695s |             363.431s |                    493.703s | VM              |

The physical Pod action's limiting duration fell by 93.566 seconds; its
completion from Bazel start fell by 98.744 seconds, including 5.557 seconds
earlier action submission. Since VM was last in the control, shortening only
Pod while holding the other branches fixed would save **zero** seconds from
that graph. The split graph's 54.414-second elapsed difference is descriptive:
VM changed by 41.599 seconds despite the same action digest and a cold
baseline, while MCP Actions slowed by 64.100 seconds. The separate
[4-CU readiness probe](https://app.buildbuddy.io/invocation/1235175c-a8c4-431d-acb6-13fc3283847b)
ran during the control graph (01:49:02–01:56:29 UTC), ending before the split
graph began. Provider load, worker preparation, and fixture setup were not held
fixed across the pair. One pair does not estimate a distribution or attribution.

This result supports case distribution as a way to shorten this Pod branch
while retaining its tested contract. It does not justify replacing the
required target yet: the selected graph still took over eight minutes, excludes
other required suites and GitHub admission/check clocks, and its VM path limits
feedback even if Pod became free. A later full-gate decision must account for
the extra stack and requested capacity under representative concurrent load.
