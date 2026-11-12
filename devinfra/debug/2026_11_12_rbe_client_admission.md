# RBE client admission in Agentplane CI

The client limit of 50 concurrent actions delayed the joint VM/Pod graph in the
2026-11-11 latency sample. At 150, 178, 190, 230, 250 and 272 seconds, its Bazel
profile had 50 overlapping `execute remotely` spans. The Pod runfiles were ready
at 178 seconds; its TestRunner action began at 277 seconds. This is client-side
admission evidence, distinct from the provider's action queue.

## Controlled runs

On 2026-11-12, an isolated checkout of
`28dae5ef3ef397393ed4df7f18425d3502ba9f77` ran this sequence:

```bash
bbr test //agentplane/... --config=ci --jobs=50 --nocache_test_results --keep_going
bbr test //agentplane/... --config=ci --jobs=100 --nocache_test_results --keep_going
bbr test //agentplane/... --config=ci --jobs=50 --nocache_test_results --keep_going
```

All 430 selected test targets ran fresh each time. Profiles use Bazel startup as
time zero; TestRunner duration includes remote preparation, execution and upload.

| Jobs       | Bazel elapsed | Pod action start | Pod action duration | VM action start | VM action duration | Peak remote spans |
| ---------- | ------------- | ---------------- | ------------------- | --------------- | ------------------ | ----------------- |
| 50, first  | 602.7 s       | 100.1 s          | 502.4 s             | 85.7 s          | 398.7 s            | 50                |
| 100        | 447.4 s       | 58.1 s           | 387.6 s             | 60.6 s          | 353.5 s            | 100               |
| 50, repeat | 607.0 s       | 212.7 s          | 392.6 s             | 217.4 s         | 321.4 s            | 50                |

Invocations: [first 50](https://app.buildbuddy.io/invocation/962a6b85-dfd8-45c3-9b92-5594c954d28f),
[100](https://app.buildbuddy.io/invocation/221d60ab-b324-4cf9-9d74-aabeae174008),
[repeat 50](https://app.buildbuddy.io/invocation/f8a5325b-f058-41ef-ab48-4e749420eb87).
Each advertises `command.profile.gz` in its build tool logs.

The Pod TestRunner action digest is identical in the 100 and repeat-50 runs:
`4fe9c5ca43e597870f353d4a143894ec1dbbfdb0c8f91463a2912e02804ca046`.
Their execution durations differ by 5 seconds while action admission differs by
155 seconds. Both runs' Pod and VM provider queues were each about 0.06 seconds.

`bbr` mirrored the unchanged checkout onto moving `devel` bases: the latter runs
report `ada9b7fc` and `646eac1d`, with reverse patches restoring the source tree.
Those stored commit labels alone are not source identity; the matching action
digest establishes the Pod comparison. Stamped outputs, outer-runner/cache state
and shared worker load remain possible differences elsewhere in the graph.

## Reliability and scope

Each invocation had one failure; all others passed:

- First 50: Runtime `TestLifecycleBarrierInterleavings/replace/setup-admission-replace`
  reported `replacement did not reopen journal`.
- 100: browser shard 4/16 failed fixture authentication with `httpx.ReadTimeout`
  on `/auth/login`, before its scrolling/projection assertion ran.
- Repeat 50: the VM tool-OOM scenario lost its exec-server WebSocket transport.

The first two runs passed every VM, Pod and MCP system suite; the repeat passed
Pod and MCP. The failures remain separate reliability work. This sequence does
not establish that any failure was caused by the job limit.

The candidate raises the RBE client limit to 100 without changing test selection,
executor resources, pool routing or caching. It targets earlier admission of the
required graph. One 50/100/50 sequence supports trying the setting, not a universal
155-second saving or a reliability-rate estimate. The measured scope is
`//agentplane/...`; exact-head PR CI covers the resulting configuration change.
