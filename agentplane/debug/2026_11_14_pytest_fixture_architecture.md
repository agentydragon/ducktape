# Agentplane fixture ownership and pytest mechanics

PR [#16489](https://github.com/agentydragon/ducktape/pull/16489) keeps the existing
required acceptance gate. The objective is 30% faster complete PR feedback with
at most 25% more compute; this refactor does not establish that improvement.

## Integration with advancing devel

The final integration base is `bbba97eb686dfee8f25ce9c64209051734e17460`.
Devel now contains `driver/cleanup.py` and the Scenario, Policy and Sandbox
callers. Keep that implementation and its distinct cancellation/failure result;
remove the experiment's duplicate `cleanup_task.py`. The remaining branch change
adopts the shared helper for private cleanup diagnostics and the reusable Pod's
owner, and extends its repeated-cancellation and failure-precedence coverage.
The two new suite recipes also use devel's explicit component props and common
fixture finalizer. Earlier run records below retain their original inputs.

## Ownership boundary

Keep exact Sandbox UID ownership, registration immediately after acknowledged
creation, uncertain-create handling, suspend/drain/delete ordering, and private
diagnostics. A generic pytest plugin cannot supply these application contracts.

Use pytest yield fixtures and Python's `AsyncExitStack` for fixture lifetime and
reverse-order release. Keep one `finish_cleanup` implementation for repeated raw
`asyncio.Task.cancel()` delivery. Scenario, Policy, and Sandbox owners previously
implemented their own cancellation loops; their callers now share the primitive.
An existing body failure retains precedence over cancellation arriving during
cleanup; cleanup-only failures remain fatal. Resource errors are aggregated by
their owners, and secondary public notes contain types/counts rather than private
exception text.

Do not carry task-group/cancel-scope contexts into the cleanup task: their exit
belongs to their entering task. The helper drains leaf cleanup operations and the
existing scenario `AsyncExitStack`, not an arbitrary AnyIO fixture task group.

## Supported pytest runner pilot

The repository pins Python 3.14, pytest 9.1.1, pytest-asyncio 1.4.0, and AnyIO
4.15.1. AnyIO already supplies its pytest plugin. Its pilot explicitly selected
the asyncio backend and passed `--asyncio-mode=strict`; root configuration retains
auto mode as its default but honors this explicit choice.

The library spike keeps an AnyIO task group alive across module-fixture setup,
two independent tests, and teardown. A negative control demonstrates that
`CancelScope(shield=True)` does not protect raw `asyncio.Task.cancel()`. It does
not justify deleting the remaining cleanup drain helper. The real platform
fixture has no task-group lifetime workaround for AnyIO to remove, so migration
is deferred. The final shared platform fixtures use pytest-asyncio's existing
module-loop pattern. Keep the isolated library spike for a future fixture that
would actually benefit from same-task setup and teardown.

References: [pytest fixture safety](https://docs.pytest.org/en/stable/how-to/fixtures.html#safe-fixture-structure),
[AnyIO pytest lifecycle](https://anyio.readthedocs.io/en/stable/testing.html),
[asyncio shielding](https://docs.python.org/3.14/library/asyncio-task.html#shielding-from-cancellation).

## Reuse and isolation

The deployment was already session-scoped: tests did not each boot K3s. The new
sharing removes smaller repeated setup within the platform target:

| Resource                                                              | Lifetime                                                                                        |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Deployment and infrastructure baseline                                | Once per Bazel test action                                                                      |
| Native clients, bound tokens, and port forwards                       | Once per platform module                                                                        |
| Healthy ordinary Pod used by three trust probes                       | Reused, exact Sandbox and Pod UIDs verified before every lease                                  |
| Failed or replaced shared Pod                                         | Discarded before another lease                                                                  |
| Impostor workload, Egress revocation and lifecycle mutation resources | Case-owned                                                                                      |
| App protocol runners                                                  | Existing pool and two-slot case concurrency retained unless the sequential experiment qualifies |

The real issuer, positive trust anchors, controller reconciliation, and Egress
assertions remain. The impostor workload in the Gateway trust test remains fresh.
The shared client fixture is deliberately local to the platform module; widening
the default fixture would mix event loops in pytest-asyncio central tests.

## Scheduler decision

`CaseBatch` preserves separate pytest identities while running case bodies with
bounded concurrency; it also transports exceptions and overrides report durations.
AnyIO's runner does not itself schedule separate pytest items concurrently.
Compare ordinary sequential pytest items using the same resource fixtures before
removing this machinery. Preserve all 15 Pod case identities and assertions.

Do not add `pytest-asyncio-concurrent` in this iteration. It replaces pytest's
fixture lifecycle; its temporary compatibility fork documents fixes needed for
pytest 9.1. `pytest-xdist` would create separate worker processes and duplicate
session fixtures. Built-in subtests change independent selection/reporting.

## Evidence and follow-up

- Control source: `8751e005b7a62591e9b43430aec0dd31aeaf16fa`, rebased on
  `fc6336654ba14c2e87b1c814d7d911ae3d5cb020`.
- [Control A1](https://app.buildbuddy.io/invocation/bb91a63a-4558-48c1-bd4a-f0f9eac1cfa3):
  both Pod/platform targets passed; Bazel elapsed 349.054 seconds. Platform action
  201.642 seconds; Pod action 295.964 seconds.
- [Initial focused checks](https://app.buildbuddy.io/invocation/793868bc-6248-47f0-a2db-7059816d2f9c):
  all seven test targets passed. The invocation failed mypy because a new test
  lacked a direct protobuf dependency; corrected in `6a067065d1`.
- [Candidate B1](https://app.buildbuddy.io/invocation/6403d81c-affa-4fd4-9097-394fb3259162)
  uses immutable source `5dd6f7cafadeca470fc9134f12be5aee89ca8a51`.
  Both targets pass in 323.360 seconds; platform action 178.793 seconds.
- [Candidate B2](https://app.buildbuddy.io/invocation/6ccd1da0-a5b1-4a38-ac3f-9c94e461d892)
  replays intended source `5dd6f7cafa` as an overlay; both targets pass in 319.967 seconds.
- [Control A2](https://app.buildbuddy.io/invocation/8a9d655f-e45e-4a92-bd07-38a01547e3e3)
  replays intended source `8751e005b7` as an overlay; both targets pass in 317.505 seconds.
- [Final focused checks](https://app.buildbuddy.io/invocation/fa68cb7a-5574-48f1-8923-0d17966b8c8b):
  seven targets / 27 JUnit cases pass, including the corrected direct dependency
  and the repeated-cancellation control. Explicit changed-library builds pass.
- [Initial sequential screen](https://app.buildbuddy.io/invocation/e55deb0a-2f99-4a13-8faa-40d448d0eb6c)
  at source `37884c809a4e5a88f40ac45658f27415d99bcbf4` plus the
  [reviewed patch](2026_11_14_pytest_sequential_spike.patch): 14/15 Pod cases pass;
  storage fails its required empty-inventory assertion after earlier cases
  populate the shared pool. Invocation elapsed 448.282 seconds. This arrangement
  does not preserve the storage precondition and is not a valid performance
  comparison. The corrected storage-first experiment is recorded separately.
- [Standard-runner shared-fixture screen C1](https://app.buildbuddy.io/invocation/68708933-c634-47c8-b7cc-1b87af9008f4)
  at `818930a8dd7b3310461387fb7809ac574bc54723`: both targets / 22 cases pass;
  elapsed 399.846 seconds, platform action 183.143 seconds, Pod action 368.363
  seconds. This differs from the earlier pilot sources and is not a matched
  control for attributing a runner effect.
- [Corrected storage-first sequential screen](https://app.buildbuddy.io/invocation/1e6c750c-892b-4106-868f-c93eff3e6909)
  uses the same `818930a8dd` base plus the
  [storage-first patch](2026_11_14_pytest_sequential_storage_first.patch). All 22
  cases pass, but elapsed is 450.625 seconds (12.7% longer than C1), and the Pod
  action is 418.267 seconds (13.5% longer). CPU is lower in this observation.
  Preserve this speed/cost distinction; the screen does not justify removing
  bounded concurrency. Worker warmth and concurrent load remain uncontrolled.
- [Expanded existing invariant checks](https://app.buildbuddy.io/invocation/78fe10ac-a7f7-45d1-bae7-1971783dd9ea):
  37 JUnit cases pass across the Sandbox and CaseBatch targets, covering UID
  replacement, draining multiple owners, secondary failures and pytest outcomes.

All four A/B runs have identical 22-case JUnit multisets, no skips, and two
passing physical test actions each. These are selected-target screens, not
complete required PR feedback. Counterbalanced order does not control worker
warmth, build-cache state, or concurrent CI/experiments. In particular, A2 is
faster than either B run: the first pair's apparent whole-graph gain does not
establish a repeatable feedback improvement.

B2/A2 are not clean exact-commit repeats. For these detached local worktrees,
the remote runner checked out `c98ee8ab9c158ff130bc2ff4b73aba1531837ace`
and applied patches. The logged patch SHA256 values are
`349b5d6350480e91b9aa95d4b39e7e272a80c82e5485fdd15da8a09dfd181d09` (B2)
and `b72ef9fcee30511674def11771edee1abf246fcc40420817f7f38fe210dc2b9e` (A2).
Bazel's advertised source-file digests for `test_pod.py` and
`test_pod_platform.py` match the intended revisions in both runs; the complete
input tree was not independently verified. Git stamping can differ. Require the
remote checkout/source proof before calling future measurements exact-commit
comparisons; a local detached `HEAD` alone is insufficient.

The source was subsequently rebased onto `c2549d48c3c3d99e05ceb831f8835f2aa07f4120`.
The two additional suite renderers now use devel's `finalize_fixture`; native
read/management accounts flow through the shared EnvironmentService authority
projection used by Gateway and Controller. Production defaults stay with that
owner. Earlier runs above remain pinned to their original source and do not
validate this rebase.

The corrected sequential experiment is rejected for this iteration. A possible
future replacement is ordinary pytest across two Bazel shards, but it must pay
for a second deployment and meet the 25% compute allowance; no saving is assumed.
For a larger feedback reduction, inspect App imports in the common pytest fixture
dependency graph and measure real historical affected-target cuts first. A
fixture helper's smaller case envelope alone cannot prove a 30% whole-PR saving.

Retain every failed action and retry. Compare JUnit case identities, physical
execution intervals, CPU, and requested compute separately. Keep missing values
unknown. Use existing OTel spans and Bazel metadata; no ad hoc timing probes.

Only expand a promising screen to the existing 30 matched PR-pair, 100 fresh
contract-execution, busy-window and four-PR-burst qualification. Track future
measurements in [#16099](https://github.com/agentydragon/ducktape/issues/16099) and
the [architecture report](https://agentydragon.github.io/ducktape/agentplane-test-architecture/).
