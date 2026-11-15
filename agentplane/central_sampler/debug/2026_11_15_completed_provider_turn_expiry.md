# Completed provider turn racing the elapsed-time limit

[PR #16651's Bazel CI](https://github.com/agentydragon/ducktape/actions/runs/43702412401)
failed only `//agentplane/central_sampler/internal/residentruntime:integration_test`
in [invocation 5f8f4e91](https://app.buildbuddy.io/invocation/5f8f4e91-e6dd-5f6b-a979-8e3ebfe16b23).
`TestRuntimeTurnLimitIncludesProviderBackoffAndAllowsNextInput` reported:

```text
command "after-limited-backoff" outcome=COMMAND_OUTCOME_UNKNOWN, want COMMAND_OUTCOME_APPLIED
```

Execution stopped with `context deadline exceeded` about three seconds after the lease was
claimed. The lease was released and the Session was not reclaimed before test cleanup. The
fixture directly constructs the runtime with a local provider and fake code host; it does not
exercise Policy admission. Recent devel target history showed passing results, but several
rows reused one cached 78.437-second execution and were not independent reproductions.

`controlledAdvance` joins canceled work before returning the deadline error, discarding the
joined result. `finishProvider` deliberately resolves ambiguous persistence with bounded
cancellation-independent database work. A terminal response can therefore be durably committed
while the turn deadline wins. Expiry settlement previously found neither a current provider
attempt nor an abandoned frontier, returned without settling, and let `FailExecution` mark the
command's effect unknown despite its completed transcript.

The repair recognizes a completed, settled durable transcript only for the exact current input
command and turn. Conversation reads and command completion retain the same owner token and
lease generation, and settlement uses the existing bounded parent context. Empty transcripts,
unresolved tool calls, pending native intents, stopped conversations and mismatched active
commands cannot use this path. It dispatches no new provider or executor work.

The PostgreSQL ambiguity regression covers both retrying persistence after cancellation and a
committed response whose reply was lost. With only the regression added, the original code
failed both cases with `expired completed provider turn settled=false, err=<nil>` in
[invocation 480486ad](https://app.buildbuddy.io/invocation/480486ad-4a8c-4d2b-8370-df82fe51e7fe).
With the repair, the full runtime integration suite (69.1 seconds) and unit suite (3.7 seconds)
passed in [invocation db55db65](https://app.buildbuddy.io/invocation/db55db65-3f30-4a9d-ad38-51bf65ba6589).
The regression also rejects a wrong command, stale owner, unfinished next input, pending tool
call and unresolved native intent, and checks that successful settlement leaves the Session
available for its next input.
