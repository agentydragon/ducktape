# Sampler rollout acceptance

The [runtime and History contracts](../SPEC.md) define shutdown, owner fencing, concurrent export
and completed-snapshot checkpoints. This record separates deployed delivery evidence from active
agent continuation.

## Exporter cutover reconciliation, 2026-10-31 UTC

Staging applied the two-replica profile before the new exporter image. At 09:25:50 a second Pod
started with `devel-20261031074751-3b19f74`. The first
`devel-20261031093550-a919091` Pod started at 09:38:06; migration 9 committed at
09:38:06.949117. Old and new Pods overlapped during the rolling replacement. The planned singleton
`Recreate` writer cutover therefore did not occur: replica-profile reconciliation and image
automation advanced independently, and the PR's ordering hold did not enforce that relationship.

Old writers could repopulate `sampler_export_checkpoint` after migration 9 deleted it. Those
markers have no writer/version provenance, so their presence alone could not establish that all
announced metadata snapshots completed under the new exporter contract. This did not establish
lost or conflicting History data; source and History cursors already matched for all 14 Sessions.

After confirming exactly two Ready `a919091` Pods, no old or terminating sampler Pods, and zero
replicas for every old ReplicaSet, reconciliation at 09:42:19:

1. Saved the 14 delivery markers for comparison.
2. Deleted only markers for `history-service.agentplane-staging.svc.cluster.local:8081` in one
   transaction with five-second lock and ten-second statement timeouts.
3. Sent `pg_notify('central_sampler_outbox', '')` in that transaction to wake the new exporters.

This repeats migration 9's delivery-only reconciliation after old writers are gone; it does not
change Goose version history. The outbox notification schedules History delivery, not agent
execution. No Session, conversation, source event, provider/tool intent or retained History row
was deleted or manually rewritten. No blocked Session was recovered or uncertain operation retried.

At 09:42:20, all 14 markers had been rebuilt. Subsequent read-only comparisons established:

- The source and History inventories contain the same 14 sampler Sessions and source IDs.
- Each source cursor equals its new delivery checkpoint and History's committed cursor, covering
  186,462 prefix entries in total. Every History metadata snapshot is present.
- All source cursors and the seven blocked flags match the pre-reconciliation values.
- Both sampler Pods remain Ready without restarts. Their inspected startup/reconciliation logs
  contain no exporter failure. Flux reported staging Ready and Healthy after the image rollout.

The new exporter writes each marker only after History covers the announced prefix and the RPC
successfully completes its metadata commit. Rebuilding the markers with only new writers resolves
the completion ambiguity left by the mixed-version interval.

## Evidence boundary

All Sessions were idle or blocked when inspected. This reconciliation establishes deployed
migration and History-delivery convergence; it does not demonstrate an active provider request or
JavaScript cell surviving the roll, or subsequent model-driven continuation. The existing blocked
Sessions retain their original evidence and require their supported, explicitly authorized recovery
paths. No live agent turn or fault-injection scenario was run for this reconciliation.
