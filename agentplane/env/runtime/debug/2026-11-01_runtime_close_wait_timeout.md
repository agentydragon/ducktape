# Runtime close wait timeout

Invocation `2bce940b-0260-4905-8bb9-db87a8508199` failed
`TestExecutionSessionAdmissionClosesWhenRuntimeQuiesces` after 10.04s with
`Runtime close: context deadline exceeded`, followed by `Host failed to join`.
The fixture gives `Runtime.Close` a 10s caller context; the available test log
does not identify which internal close or join stage consumed that deadline.

The later uncached focused run `14fd3c3a-c201-41f1-a35f-79436ae36dd6` passed
both `//agentplane/env/runtime:runtime_test` and
`//agentplane/env/supervision:supervision_test`. That pass does not establish
the cause of the earlier timeout or prove this change fixed it. If it recurs,
capture stage-level close/join evidence before attributing it to a component.
