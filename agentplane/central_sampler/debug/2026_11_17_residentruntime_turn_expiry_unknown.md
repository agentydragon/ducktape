# Resident runtime turn expiry marked a later input unknown

In [PR #17433's scheduler test invocation](https://app.buildbuddy.io/invocation/36b4c9f5-f1fd-5012-9477-18e2e8de6c64),
`TestRuntimeTurnLimitIncludesProviderBackoffAndAllowsNextInput` failed because
`after-limited-backoff` was `COMMAND_OUTCOME_UNKNOWN` instead of `COMMAND_OUTCOME_APPLIED`.
The log records `context deadline exceeded` for the runtime execution, but does not record
whether `execute` entered its expired-turn settlement branch or which settlement check returned
false.

The runtime fixture starts `Runtime.Run` with a cancelable background context. That makes an
outer test deadline unlikely, but the old log does not capture either context's `Err` at the
decision point. If expired-turn settlement ran, the existing code can mark the command unknown
only after the provider-attempt/frontier paths fail to settle it and
`finishCompletedInputTurn` rejects the transcript or Session state. The old invocation contains
no state snapshot to identify which condition occurred. The mechanism is unproven; the change in
PR #17444 is diagnostic-only and must not be treated as a behavior fix.

The production driver now emits a structured `slog.Warn` when it skips or declines expiry
settlement. If this test fails again, find the warning for `after-limited-backoff` in the
`scheduler_test` log. Its `reason`, `turn_context_error`, `execution_context_error`, and `expired`
fields show which expiry path ran; `current_attempt_command_id` or
`recovery_frontier_command_id` identifies provider state; `completion_guard` names a rejected
completed-turn check; and `session_active_command`, `session_active_turn`, and `session_blocked`
show the Session state when a Session guard fails. The mechanism remains unproven until a failure
log identifies the deciding condition.
