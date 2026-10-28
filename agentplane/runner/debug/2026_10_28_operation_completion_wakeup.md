# Operation completion wakeup

PR #12694's head `17db03b979` failed
[`test_codex_code_mode`](https://app.buildbuddy.io/invocation/c62fdf75-7ae1-5450-b029-43a4f92541bb).
The successful shell case finished; the failing-shell case timed out waiting for progress.
Its retained native journal contains only command admission and Codex's initial
`remoteControl/status/changed` notification. The Runner log has no controller errors,
and no reconciliation between 16:20:10 and test teardown at 16:24:34 UTC.
Three fresh executions of the same head passed, so this is intermittent.

The shared operation implementation publishes completion before closing `Done`.
A COSI controller can consume that notification before `Done` closes, find the
operation unfinished, and return. The worker's later host-input refresh can be a
no-op, leaving no event to revisit the completed operation. This race can stall
bootstrap, shared Codex startup, or Session preparation. The retained CI artifacts
cannot identify the exact operation or prove that this interleaving happened.

The targeted regression from #12701 holds completion publication open while a
consumer attempts to inspect the operation. It fails against the old implementation:
the consumer returns before publication finishes, leaving the slot unconsumed.
[Before-fix result](https://app.buildbuddy.io/invocation/912bf455-33b6-4bad-925f-b837b4ab7f5e).

Reuse #12701's completion-ordering correction: publish completion and close `Done`
while holding the owner's slot mutex, which `Consume` also takes. This preserves
publication before consumption while preventing the missed wakeup. Completion
callbacks run outside that mutex. Initial-publication failure handling changes
remain in #12701.
