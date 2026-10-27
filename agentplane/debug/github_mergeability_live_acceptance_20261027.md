# GitHub mergeability live acceptance — 2026-10-27

Live operator exercise for [PR #12274](https://github.com/agentydragon/ducktape/pull/12274), merged
as `fdf7c8073fa96f5b04dc61bc0883d76d771c1bf7`. The live exercise verified delivery and
periodic repair, but acceptance fails prompt detection and current base-SHA labeling.

## Environment and fixture

Notifications ran `devel-20261027222401-9291858`, a descendant of the feature merge, with 2/2
updated, available and ready replicas. Both migration init containers previously exited 0.
The exercise uses this machine's kubeconfig, an isolated `runner-ducktape` Sandbox with only the
`basic` PolicySet, a fresh Codex Session on GPT-6 Luna at low effort, and a Pod-bound workload
bearer for the Notifications API. Credentials remain in memory.

The disposable draft [PR #12328](https://github.com/agentydragon/ducktape/pull/12328) targets
`acceptance/mergeability-accept-745b4d82-base` from
`acceptance/mergeability-accept-745b4d82-head`. Both branches have a small synthetic fixture
history independent of `devel`; the exercise does not update `devel` or user branches.

Sandbox: `mergeability-accept-745b4d82` (`fed97f48-9fd4-4346-b496-7b27108276fb`).
Session: `8add49d4-0dca-46f9-a23e-7735436cc622`.
Inbox: `19f1a8f0-de01-450e-9133-ef9496bc01b1`.
Transition subscription: `e5ffc5d7-473d-43f7-867d-e87dd7f3b976`.
Only `mergeability_changed` is selected, isolating generated observations from raw PR webhooks.

## Base-only change

Before the push, GitHub reported mergeable and the store held `OPEN` / `MERGEABLE`, snapshot
version 1. The inbox was empty. PR head was
`0bdf5ad683ed31ab1457242127ae9fd57afacc9e`, and base was
`fae8b28f798cbd0d09d51bfc2cb6d4746f7f59de`.

The base branch alone advanced to `0544f4f072bc5566cdf6b3520ba8a3ff1c1df4ef`, changing the same
line as the PR head. The push webhook was persisted at **23:23:18.449724 UTC**. Agentplane's
subsequent observation at **23:23:18.950409 UTC** still recorded `MERGEABLE` and the old base
SHA. It recorded successful source contact and no early retry deadline, retaining the ordinary
600-second subject freshness interval.

At **23:26:37 UTC**, GitHub's REST PR endpoint reported `mergeable: false` / `dirty`, but the
store still held the earlier mergeable result and the inbox remained empty. This was a received
webhook followed by a stale successful API read, rather than a missed webhook or failed read.
Generation fencing does not reject it: the push arrived before the refresh began.

The REST PR response still reported `.base.sha = fae8b28...` after returning `mergeable: false`;
the current branch ref was `0544f4f...`. GraphQL likewise exposed the old `baseRefOid`, while
`baseRef.target.oid` exposed the current branch tip. Thus the PR's stored base field is insufficient
to label the current branch revision used for a mergeability check in this exercise.

GitHub's [REST PR documentation](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)
describes asynchronous mergeability and polling when the result is null. The exercised API read
returned an old definite answer, so the implementation's UNKNOWN-only early retry did not help.

Raw scoped evidence is in `/tmp/pr-12274-live/`, including `state.json`,
`baseline-observation.json`, `base-push-stale-result.json`, and `deployment.json`; no credentials
are saved there. No refresh timestamps or database rows were altered to force source progress.

## Periodic repair, notices and late subscription

At **23:33:19.304025 UTC**, normal subject refresh emitted exactly one
`MERGEABLE → CONFLICTING` entry: inbox cursor 1, transition sequence 3. This was **600.854 seconds**
after the push webhook arrived. No intervening PR change or forced refresh produced it. Its
`baseSha` was still the original `fae8b28...`, rather than the actual base tip `0544f4f...`.
This proves periodic mergeability recovery, but not prompt recovery from the received push or
recovery of other GitHub event kinds.

The first notice command `32e50e1a-03ca-4390-8197-c9206edd29f0` was admitted at
**23:34:19.755236 UTC**. It started the previously dormant harness, which confirmed the input at
**23:34:31.874984 UTC** and completed the turn successfully at **23:34:38.082711 UTC**.
The entry-to-admission delay includes the configured 60-second notice debounce.

The head then advanced, still conflicting, to `cd49c3fd72698850f5819eaca5f06ef3d06ea1e8`.
It produced no duplicate mergeability transition. A second subscription,
`5a96295b-f2fc-4156-a139-70e148beff59`, received one initial snapshot at inbox cursor 2,
`snapshotVersion = 4`, containing that current head rather than the head from transition 3.
Its base SHA was still stale, so the full current-revisions criterion **failed** despite the
current-head and independent-snapshot behavior passing. Notice command
`1c2f26f2-1eb8-40de-9f0a-973d5425f345` was confirmed at **23:35:37.025088 UTC**, followed by a
successful completed turn at **23:35:39.657884 UTC**.

## Resolution

A resolution merge commit, `00368d8d73d5d70d6ab3cb603aee378f88da331c`, included both the
conflicted head and the advanced base as parents. A transient TLS-handshake timeout interrupted
the first ref-update request; the operator probe read the current ref, retried the same commit
without force, and verified the resulting ref before proceeding.

At **23:36:15.521441 UTC**, the service emitted one `CONFLICTING → MERGEABLE` entry at inbox
cursor 3, transition sequence 4, associated with **both** subscriptions. It carried the correct
resolution head and base tip. GitHub also reported mergeable. Notice command
`b2bfbf01-c5f9-4ab8-b2a5-34fc38e78b3f` was admitted at **23:37:16.156284 UTC**, confirmed by
the harness at **23:37:19.661356 UTC**, and its turn completed successfully at
**23:37:22.554668 UTC**. Runner evidence for all three notices is retained in
`/tmp/pr-12274-live/runner-events.json`; final inbox evidence is in
`final-inbox-before-cleanup.json`.

## Outcome and remaining work

- Passed: webhook receipt, persisted initial mergeable state without an entry, ten-minute periodic
  conflict recovery, conflict/resolution notices reaching a real harness, successful model turns,
  no duplicate transition for a changed head that remains conflicted, an independent initial
  snapshot with the current head, and one resolution event matching overlapping subscriptions.
- Failed: prompt post-push detection when GitHub initially returns a stale definite answer, and
  current base-SHA labels on conflict and initial-snapshot payloads.
- Not exercised: intentionally suppressed webhooks, induced provider failures, restart injection,
  historical event backfill or retention expiry. Previously waived broad outage/refresh exercises
  are not reopened by this report.

The immediate fixes are durable follow-up after a stale definite post-change answer and current
base-revision labels. Broader missed CI/check, comment and review recovery still needs a reviewed
contract. This exercised mergeability path does not establish historical webhook completeness
or broader state reconciliation.

## Cleanup

Both fixture subscriptions were cancelled through the Notifications API after all three entries
and the final notice receipt were checked. The inbox was explicitly acknowledged through cursor 3;
no unrelated inbox was retired or acknowledged. The fixture PR was closed without merging, and
both temporary branches were deleted with leases on the exact observed SHAs. The owned Sandbox
was suspended, its effective `Suspended` mode and original UID verified, and its deletion requested.
A final namespace read confirmed no matching Sandbox, Pod, PVC, ServiceAccount, ConfigMap or
Service remained. The local Notifications port-forward was stopped. The synthetic archived Thread
and normal retained inbox/source records remain under their ordinary retention policies; no
database purge was performed.
