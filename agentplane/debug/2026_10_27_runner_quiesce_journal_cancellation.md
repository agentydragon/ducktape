# Preparation cancellation can fail the runner journal

The dispatcher wake test in [invocation b72b1392](https://app.buildbuddy.io/invocation/b72b1392-1f2d-5eb1-81ed-e922cbfb37f0)
reached runner admission, then timed out stopping the Sandbox. Its cleanup artifact shows a pending command,
no active turn, and a journal latched with `context canceled`. The same head passed on retry.

The captured runner log contains only its last 200 lines; native executor logs and the journal itself
were not captured. That prevents identifying the first failed operation in this particular CI run.
The Lease's `draining` value covers every internal lifecycle stage before flushing; it does not identify
which internal stage stalled.

## Reproduced mechanism

1. A command is durably admitted while preparation is waiting on a native handshake.
2. Quiesce cancels preparation and joins it.
3. The native reply wins a simultaneous reply/cancellation race and the handshake returns successfully.
4. `Session.prepare` persists `HarnessStarted` using preparation's canceled context.
5. SQLite rejects the write. `Journal.write` latches that failure; later command settlement and sealing
   cannot use the journal. The runner remains in its internal halting stage.

The focused Go reproduction failed against unmodified production code in
[invocation a5a13662](https://app.buildbuddy.io/invocation/a5a13662-1281-42a5-af51-edc1e9322f34):
`journal failed; reopen it for recovery: context canceled`, internal stage `2` (`stageHalting`).
It uses real journal/admission/quiesce code and a controlled handshake boundary. This confirms a bug
matching the CI signature; it cannot prove that the archived CI run hit this exact interleaving.

Retained-response recovery has the same lifetime issue: both native adapters can translate stored
responses into journal mutations while preparation is being canceled.

## Candidate

Keep completed native observations, recovery translation, and startup frame persistence on the runner's
lifetime. Continue canceling native I/O and response waits with preparation. Finish retained-response
translation before honoring cancellation and starting any new harness work. Keep journal failure
latching intact; clearing it would ignore uncertainty about storage and already-mutated adapter state.

The initial handshake case passed with the candidate in
[invocation d7c4d1ec](https://app.buildbuddy.io/invocation/d7c4d1ec-3f63-4efe-8834-7e001cb60d16).
The regression also checks settlement of the undispatched admission, sealing, exact ingress
acknowledgement, and completed quiesce. A second case now covers retained-response translation.
Both cases passed in [invocation a502bec3](https://app.buildbuddy.io/invocation/a502bec3-a622-49e3-aeae-28ae1985559f).
The complete race-enabled runner Go suite passed with the final candidate in
[invocation cb8062cd](https://app.buildbuddy.io/invocation/cb8062cd-dd9f-4cc3-bbe4-4299108fa4a0).

The original CI evidence and extraction note are retained under
`/tmp/ducktape-pr12232-run40636676607-*`.
