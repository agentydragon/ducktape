# Retiring the legacy GitHub CI tables

The 2026-10-28 staging audit after native-storage cutover found that `github_ci_event` sequences
2112–2126 also exist in `github_commit_event`, but identify different observations. The old rows
were created at 05:11:51.642713–05:12:21.085610 UTC, after Goose migration 7 was recorded at
05:11:50.982202 UTC. The colliding native rows were created at 05:12:07.097090–05:12:47.591994 UTC.
This suggests old workers continued writing after the copy while native sequence allocation reused
those IDs. The legacy rows remain present; migration 9 did not remove them.

Only two of the 15 pairs have the same App, installation, repository, head and event key; none has
an identical payload. Eleven legacy current-fact keys have no native counterpart. One retained
`github_observation` inbox identity refers to a conflicting sequence/key combination. Comparing
sequences alone would therefore silently discard facts or change the meaning of a stored identity.

Before dropping `github_ci_*`, reconcile each conflicting event with the retained inbox identities
and payloads, assign unambiguous native identities where needed, and verify subscription boundaries
and pending snapshots. Preserve the old rows until that check succeeds. Current runtime writes use
native tables; retention still deletes expired legacy events/current facts. Normal configured
retention continues to apply while reconciliation is pending.

The final-schema baseline deliberately keeps the legacy tables and columns. Its removal of the
version-9 conversion machinery is independent of this older GitHub copy issue.
