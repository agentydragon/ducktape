# Unresolved sampler drain fixture sampling failure

The [failed invocation](https://app.buildbuddy.io/invocation/3f86caf9-825e-57cf-906f-e9f081928e02)
for PR #17050 failed `TestRuntimeDrainDeadlineCancelsUnsettledCellAndSuccessorSettles` in
`//agentplane/sampler/internal/residentruntime:integration_test` on 2026-11-16.
The cause and whether the failure predates the refactor remain **undetermined**. Passing later
checks do not resolve this failure. No production fix has been made.

## What failed

The test reported `runtime_lifecycle_test.go:538: the owner did not sample the model after the cell yielded`
after 5.740 seconds. Line 538 is the helper call. The assertion is in
[`drainWithDispatchedNestedCommand`](../sampler/internal/residentruntime/runtime_lifecycle_test.go).
It happens before that helper cancels the runtime into drain. The test therefore did not exercise
the drain deadline or successor settlement in this invocation.

The fixture sends a nested `exec_command`, an `execution/started` response, and a yielded initial
observation. It then waits five seconds for its HTTP provider handler. Sending the yielded frame
does not prove that the driver accepted it, committed the cell result, or prepared a provider request.

CI built synthetic merge `162a46c1e392ceeccb3ae86a4f628d96c38003be`, whose parents are
`811320408e8ab3647e060d974820879c373e7401` and PR head
`9da41e5469cd8fc1ea8586da921b30f2a318e868`. Its sampler tree equals the PR head's sampler tree.
Concurrent base changes in that merge do not explain this failure.

## Retained evidence

All four advertised test artifacts were downloaded: `test.log`, `test.xml`, the undeclared-output
manifest, and `TestRuntimeDrainDeadlineCancelsUnsettledCellAndSuccessorSettles-001-code-mode-host.log`.
The host log is empty. The test log and XML retain only the assertion for this test; they do not
retain its driver error, binding error, conversation state, or goroutine stacks. The absence of
those diagnostics prevents selecting a cause from the possible failure paths.

The November 9–16 BuildBuddy target-history search found no earlier instance of this assertion.
Because fork CI disables target tracking, the investigation also enumerated 3,241 failed test
invocations and queried their failed central-sampler targets, with no API errors. Downloaded logs
from 31 failed current or older monolithic sampler targets contained no earlier matching assertion.
This census selected failed targets; it is not a complete census of timeout or failed-to-build statuses.
Absence of an earlier matching failure does not prove introduction by #17050.

Earlier failures included:

- [Devel, November 10](https://app.buildbuddy.io/invocation/f66774e9-4fdb-47f4-8869-1f2e88edd628),
  commit `56648fcb13730d19bfbac9ef18f1140617571ba3`: both drain cases failed waiting for
  `session/wait`. This is a later fixture phase and does not establish the same mechanism.
- [Devel](https://app.buildbuddy.io/invocation/480486ad-4a8c-4d2b-8370-df82fe51e7fe), commit
  `37346bafb55c9d24ed41a9fd3981254bf8011309`: provider persistence recovery assertions failed.
- [Another PR](https://app.buildbuddy.io/invocation/5f8f4e91-e6dd-5f6b-a979-8e3ebfe16b23), commit
  `6ef58d5b6f4149a7cce114db606181acf503678d`: the provider-backoff turn-limit case failed.

The older [shutdown lease race](../sampler/docs/initial_validation.md#graceful-shutdown-lease-regression)
occurred during ownership acquisition or release. This failure occurred before the fixture started drain.

## Mechanisms examined

The available evidence does not identify which step between the yielded frame and provider entry
failed. Static inspection found no lock cycle from the blocked `StartProcess` call to model
sampling; this does not establish the cause of the failed invocation.

A [controlled diagnostic invocation](https://app.buildbuddy.io/invocation/c58dabd2-8f53-41fc-a445-2ad2655dfd2f)
forced three orders: yield before the executor open response; yield after start dispatch; and
provider entry before the nested delegate starts. All reached the provider and drain. This was a
modified diagnostic test, not a rerun of the failed assertion. Those passing executions do not
establish which interleaving occurred in the failed invocation or explain its cause. Temporary
probes were removed.

The original artifacts do not show whether the host accepted the yielded frame, its journal writes
completed, or the driver prepared a provider request. The cause remains unknown.

## Follow-up diagnostics

The drain deadline fixture now captures runtime error logs, the bound Code Mode error, bounded
read-only snapshots of the conversation's durable tables, goroutine stacks, and the host stderr log
when the provider-entry gate fails. Its failure defer runs before deferred runtime cleanup, and its
database capture still runs if the driver has already released ownership. Existing deadlines and
assertions are unchanged. A deterministic capture test verifies these artifacts after an injected
binding failure; it validates the diagnostics and does not establish the cause of the original
invocation.
