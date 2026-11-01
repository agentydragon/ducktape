# Queued input hidden by a stopped conversation

## Observation

[Hosted invocation](https://app.buildbuddy.io/invocation/21bad782-a3c3-5f95-8303-21162f17078d)
failed `test_turn_outcomes_preserve_safe_continuation[429]`. The first mocked HTTP 429 was durably
rejected, the first command failed, and the Session remained unblocked. After a second input was
admitted, the endpoint saw no new provider request within its existing ten-second deadline.

The sampler log records one lease claim, the known rejection, then shutdown with zero active
drivers. It does not capture the precise ordering of second-input admission and driver retirement;
the original run alone cannot establish that interleaving.

## Mechanism

`Reject` marks the previous conversation turn stopped. `Admit` stores a new accepted command
without clearing that flag; `NextCommand` clears it when it actually processes the input. Both
`OwnedWork` and `RecoverableSessions` formerly required `NOT c.stopped` before checking accepted
commands. They therefore missed legitimate queued input after a stopped turn.

A direct wake can hide this defect by immediately launching a fresh driver. A wake delivered to a
driver already retiring can be lost when that driver exits, leaving neither scan able to find the
input. Admission on another replica also depends on `OwnedWork` waking an idle resident after an
ordinary completed turn. Notifications and periodic reconciliation must use durable pending work,
not whether the previous turn stopped.

## Deterministic check and repair

`TestWorkScansRetainAcceptedInputAfterKnownRejection` uses the real PostgreSQL store to reject the
first sampling intent and admit a second input. It checks both scans with a live owner, a released
lease, and an expired lease. All three cases failed on the original predicates in
[the regression run](https://app.buildbuddy.io/invocation/e427f6ce-d9a1-4241-8c10-cf7431bd9b86):
both scans returned empty lists.

Remove the stopped-turn predicate from both scans. Pending work and lease ownership still determine
eligibility; blocked Sessions remain excluded. This changes no admission, provider retry, intent
replay, or failure-settlement policy. The regression also verifies that the settled first rejection
alone has no work to schedule.

After the repair, the full Go sampler suite and Python session-correctness suite both passed in
[the focused validation](https://app.buildbuddy.io/invocation/ed2ec974-ebb4-4b8e-9a12-aba64fbeb89d).
No timeout was increased and no provider request was replayed by this change.
