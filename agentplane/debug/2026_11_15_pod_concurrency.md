# Pod case concurrency with fixed memory settings

The experimental branch defaults its App Pod suite to four workers. Six fresh
actions at two, three and four workers all passed the same 15 cases. Pod resource
requests and limits, case bodies, declaration order, exclusive storage ordering,
fresh Sessions, worker ownership, timeouts and the 4-CU executor request were fixed.
Native-platform acceptance remains a separate required target.

Measured source: `49d109ff43eee720bfd84548330131bbbea0bd8d`, based on
`aaa39328e439c7ceda96588ef7bd9b0d55c0125e`. The permanent
[benchmark source tag](https://github.com/agentydragon/ducktape/tree/agentplane-pod-concurrency-20261115)
retains this source through later rebases. Only `AGENTPLANE_POD_CASE_LIMIT`
differed between the action Commands. Their complete input-root digest was
`e096325647f5e93c7fa963f8c942c68d8a9f1e3fdfb52503db62ad9f0c040caf/165`;
executor properties and canonical JUnit identities were identical.

## Results

Order: 2, 3, 4, then 4, 3, 2. Every action reused a warm infrastructure baseline,
created a distinct namespace, database Pod, root Certificate/Secret and collector
namespace, and certified cleanup complete. Each invocation indexed one NEW
TestRunner and one passing JUnit attempt. Internal executor retries are not fully
observable from those records.

| Workers | Fresh passes | Action seconds, individual | Mean case envelope | Mean CPU seconds | Maximum action memory |
| ------- | ------------ | -------------------------- | ------------------ | ---------------- | --------------------- |
| 2       | 2/2          | 312.4, 295.5               | 207.9s             | 737.1            | 4.95 GiB              |
| 3       | 2/2          | 245.2, 276.7               | 170.0s             | 727.4            | 4.99 GiB              |
| 4       | 2/2          | 238.7, 238.9               | 159.0s             | 689.9            | 4.87 GiB              |

Four workers averaged 238.8s versus 304.0s at two: 65.1s / 21.4% less action
time and 6.4% less CPU. The case envelope fell 49.0s / 23.6%. Setup independently
averaged 17s faster in the four-worker arm, so the full action difference cannot
all be attributed to concurrency. No 30% whole-PR feedback saving is established.

Kubelet samples retained at least 4.70 GiB available across the six actions. The
four-worker arm sampled up to five runtime Pods at once. Summed Pod working sets
peaked below 0.70 GiB in every arm, versus roughly 5 GiB whole-action memory;
K3s/containerd, tmpfs image layers and other node overhead are outside those Pod
working sets. The before-import/final diagnostic snapshots contain no
FailedScheduling, OOMKilled or eviction reasons. Sampling is every ten seconds;
these are not exhaustive instantaneous peak or reliability-rate measurements.

## Immutable runs and reproduction

- [2 first](https://app.buildbuddy.io/invocation/4a2fe3e6-2cc4-472c-8d52-35bc1dd976d9)
- [3 first](https://app.buildbuddy.io/invocation/ca8e5bfa-0624-47a8-9500-de75cc6a2cad)
- [4 first](https://app.buildbuddy.io/invocation/a615d9b0-5cc9-4402-ae93-28c001c9f43c)
- [4 repeat](https://app.buildbuddy.io/invocation/077f1c1c-4399-4abe-b8bf-ef07df4148f3)
- [3 repeat](https://app.buildbuddy.io/invocation/7a8c7d2b-6690-4696-a25c-0b74f2209e23)
- [2 repeat](https://app.buildbuddy.io/invocation/4ed32eb8-6655-422a-8c95-a07558581322)

For each worker count, sequentially in the stated order:

```sh
bbr --run_from_branch=agent/native-pod-services-partitions \
  --run_from_commit=49d109ff43eee720bfd84548330131bbbea0bd8d test \
  //agentplane/system_tests/suites:test_pod \
  --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1 \
  --test_env=AGENTPLANE_POD_CASE_LIMIT=4 --test_output=errors
```

Use `bbapi execution <invocation> --json` for physical action clocks, CPU and
peak memory. Compare the JUnit identities, `test-spans.jsonl`,
`memory/working-sets.jsonl`, `memory/peaks.json`, `persistence-suite.json` and
storage diagnostic snapshots. Raw Commands, logs and fixture artifacts stay private;
the report publishes reviewed aggregates and source/invocation links.

## Decision and follow-up

Retain four workers on this branch. Memory limits need no change for this screen.
The environment override remains so later comparisons can use identical source.
The ordinary-pytest two-stack experiment remains rejected; this intervention
increases parallelism inside one shared stack.

The independent devel port [#16659](https://github.com/agentydragon/ducktape/pull/16659)
merged as `b83d8b0f00cda1729fce43fc3085f343093bd1a2`. Its full 22-case suite passed
four matched 2/4/4/2 actions: mean action 363.3s to 289.2s, with unchanged resource
settings. One control's trust probe took 94.5s rather than 12–17s; against the faster
control alone, the four-slot mean was 13.8% shorter. These observations remain
separate from the 15-case screen above. The
[reviewed report](https://agentydragon.github.io/ducktape/agentplane-test-architecture/#devel-pod-concurrency)
contains its pinned source, all four immutable invocations and independent passing CI.
Four workers and the environment override are now inherited from devel.

Measure selected-graph and complete required feedback under concurrent CI load;
another suite can remain the critical path.
Keep cold-baseline and repeated fresh failure observations separate from this
six-action warm screen. Track those follow-ups in
[issue #16099](https://github.com/agentydragon/ducktape/issues/16099).
