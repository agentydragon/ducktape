# CI architecture branch after the native ports

PR [#16489](https://github.com/agentydragon/ducktape/pull/16489) is rebased onto
devel `a4c8ee98430d365c237954bde3ca3767dfa844bd`. The required test architecture
is now on devel. This draft retains the manual ordinary-pytest comparison targets
and experiment records; it has no remaining production authentication, required
suite, native-client or resource-pool implementation change.

## Landed implementation and evidence

| Change                                                  | Merged PR                                                     | Checked source and CI                                                                                                                                                                   |
| ------------------------------------------------------- | ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Explicit App fixtures and neutral Sandbox definitions   | [#16675](https://github.com/agentydragon/ducktape/pull/16675) | [e874dc89e2](https://github.com/agentydragon/ducktape/commit/e874dc89e2fbc0b4a17614123a316d23e68270d6), [CI](https://app.buildbuddy.io/invocation/8d7d28e3-d13a-5de8-9285-7acebb5da064) |
| SVID-backed native clients and exact resource ownership | [#16689](https://github.com/agentydragon/ducktape/pull/16689) | [1183f536f7](https://github.com/agentydragon/ducktape/commit/1183f536f75b93de8fa123adc34dc28f2fa2b37c), [CI](https://app.buildbuddy.io/invocation/bada925e-ab64-5e59-9778-73078ff2b8f8) |
| Acceptance suites split by their actual dependencies    | [#16695](https://github.com/agentydragon/ducktape/pull/16695) | [33dd5b6317](https://github.com/agentydragon/ducktape/commit/33dd5b631759b50f1c7f99ff1b952f4ffc9f3646), [CI](https://app.buildbuddy.io/invocation/8dc2680b-c7af-5fbd-abff-e37be13f988b) |
| Module clients and validated trust-Pod reuse            | [#16700](https://github.com/agentydragon/ducktape/pull/16700) | [e159531320](https://github.com/agentydragon/ducktape/commit/e159531320921ae562ce76d0c3513437cf90c669), [CI](https://app.buildbuddy.io/invocation/f86aeaa8-7807-56c6-97b2-8333148298bf) |

The suite-split CI passed all fourteen selected test targets. Runtime JUnit
inventories are 16 App plus seven native Pod cases, and one App Action plus three
MCP plus four native central cases. The new Runtime proxy witness is among the
App cases. The final trust-Pod CI executed all seven native cases remotely with
no failures, errors or skips; its pool unit check was a remote cache hit.

The extracted implementations supersede the prototype's older ownership and
pool code. The native clients reuse devel's existing `app-workload-tls` certificate;
the rejected Controller bearer opt-in and the extra certificate are not replayed.
Devel also owns the four-worker Pod default from
[#16659](https://github.com/agentydragon/ducktape/pull/16659).

The independent Bridge reliability fix
[#16704](https://github.com/agentydragon/ducktape/pull/16704) is merged: await the
Claude-owned transcript before deliberately killing its harness while History
ingress remains fenced. [Focused recovery checks](https://app.buildbuddy.io/invocation/f85c63b4-764b-41d2-a75f-6eb97bf62d77)
and [required CI](https://app.buildbuddy.io/invocation/20ded4a3-06ad-5000-a201-ce092b630cf8)
passed. The earlier VM API/Controller outage remains an unresolved diagnosis in
[#16099](https://github.com/agentydragon/ducktape/issues/16099#issuecomment-6894330879);
a later pass is not proof that its root trigger was repaired.

## Remaining experiment

The manual `test_pod_sequential_spike`, `test_pod_sequential_a_spike` and
`test_pod_sequential_b_spike` targets call devel's scenario functions through
`Worker.open`. They retain marks and run exclusive fresh-inventory checks first.
The split uses a selector and its complement, so devel's additional Runtime proxy
case joins the second shard. Required acceptance still uses devel's `CaseBatch`.

The two-stack allocation was rejected at its
[measured source](2026_11_15_app_fixture_cut.md#ordinary-pytest-experiment):
6.90% longer maximum test execution and 45.58% more observed TestRunner CPU than
its same-source control. Those results cover fifteen App cases at their frozen
source, not the current sixteen-case inventory. The retained targets are
reproduction tools, not an adoption proposal or a new performance result.

The AnyIO lifecycle pilot remains available at its
[immutable source](https://github.com/agentydragon/ducktape/blob/22f982f5d9557f9844eb9058d62b79ae1e4e5754/agentplane/system_tests/driver/test_async_fixture_lifecycle.py).
The current diff does not carry that test or its global strict-mode override.
Older fixture, selector, scheduler and concurrency records retain their original
source pins and invocation URLs. Their retained findings remain distinct from
the new port correctness checks.

## Measurements still needed

The 30% complete-required-feedback improvement with at most 25% additional compute
has not been established. The four-worker single-cohort result and earlier
79-to-15 App Client selector screen are historical evidence, not new port deltas.

[#16099](https://github.com/agentydragon/ducktape/issues/16099) tracks configured
image/runfiles inspection, complete historical PR selection with positive controls,
matched full-gate runs that include queue/setup/cleanup and added stack costs,
and fresh-resource versus shared-trust allocation/timing comparisons. Keep the
seven-day concurrency exposure window ending 2026-11-22T11:01:01Z separate.
Another scheduler or contract-architecture experiment should first test a specific
plausible path to that complete-feedback objective; the rejected shard allocation
does not warrant a larger repeat.
