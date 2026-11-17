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

The diagnostic artifact added by PR #17444 records the turn and execution context errors, the
`expired` decision, any provider attempt and recovery frontier, the completed-transcript guard
that rejected settlement, Session active command/turn state, provider request counts, and final
command outcome. If this test fails again, inspect
`provider-turn-expiry.json` under its undeclared test outputs before proposing a behavior change.
