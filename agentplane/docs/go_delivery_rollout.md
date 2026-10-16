# Go delivery rollout

The environment root owns CNPG, the Dispatcher database/Secret, both stable Services, and the
Goose Job. A separate `agentplane-{testing,staging}-delivery` Flux Kustomization owns the Go
workload, its account, RBAC and policies. The serving dependency checks the root's current
generation, Ready condition, suspension state and exact migration Job identity. An earlier
healthy root cannot admit a new serving release before its migration is accepted.

The rollout configuration suspends both environment roots. Publishing it does not activate Go:
existing Python workloads continue running until the coordinated cutover. Resume testing first;
staging remains held until testing acceptance. Source suspension must be released in a reviewed
activation change, rather than relying on an imperative resume that GitOps can overwrite.

## Preparation

1. Merge and reconcile the pruning-protection change before changing ownership. Inspect the
   live Gateway account, RBAC, policies and staging PDB for
   `kustomize.toolkit.fluxcd.io/prune: disabled`. The two Services and database keep their root
   owner throughout. Record their UIDs and the root's applied artifact revision.
2. Merge the staged rollout configuration. Confirm both roots are suspended and each delivery
   Kustomization is blocked by its root. Inspect each root and serving ExternalArtifact to ensure
   the staged manifests and pinned images are available before releasing suspension.
3. Recheck the published serving/migration digests in `cluster/cdk8s/agentplane/runner_gateway_go.py`.
   These are a reviewed release pair. Image discovery does not advance them automatically.
4. Follow [state preservation](staging_data_preservation.md): capture the existing Deployment
   manifests/images, resource identities, queue/opening identities and states, and backup/restore
   evidence. Verify the Dispatcher database's sole Alembic head is
   `0007_drop_opening_attached`. The Goose preflight also verifies columns and indexes and refuses
   a different baseline. Do not log payloads, tokens or credentials.

The schema handoff's automated proof is
`//agentplane/runner_gateway/go/migrations:test_migrations`, including retained queued work and
idempotent application. It does not replace the environment's backup/restore check.

## Fenced activation, one environment at a time

A brief delivery outage is expected. Do not accept both Python and Go writers concurrently.

1. With the root still suspended, scale the Python `session-dispatcher` and `runner-gateway`
   Deployments to zero. Wait for their Pods, including terminating Pods, to disappear. Delete the
   two stopped Deployment objects and verify they stay absent. Leave runner Pods/VMs, their state
   PVCs, stable Services, connection EndpointSlices and queue rows intact.
2. Verify the old delivery and connection Lease holders no longer renew; wait for their recorded
   expiry rather than deleting queue ownership rows or inventing a new command ID. Confirm no
   old workers can be recreated from an earlier artifact.
3. Release this environment root's source `suspend` in the reviewed activation change. Reconcile
   the new root artifact. Its full-digest-named Goose Job must complete; the serving dependency
   then admits the pinned Go image. The root prunes obsolete Python configuration/account/RBAC,
   while protected Gateway resources are adopted by the delivery owner.
4. Verify original Service/database UIDs, schema/queue identities and migration completion.
   Existing runner Pod and VM connections must recover. Check app/Notification live reads and
   controller Quiesce, retained command delivery, lost-response same-ID retry, replica ownership
   routing/failover and UID replacement fencing. Keep `Recreate` until acceptance is recorded.

The bounded model-free testing probes and their cleanup/evidence contracts are in the
[acceptance guide](../acceptance/README.md#dispatcher-queue-and-replica-acceptance):

```bash
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_arg=-k --test_arg=queue --test_output=streamed
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_arg=-k --test_arg=replicas --test_output=streamed
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_arg=-k --test_arg=opted_in --test_output=streamed
```

These need the operator host's credentials; retain remote execution for their actions.
Testing's probes do not establish staging acceptance or replace reconnect checks for existing
stateful runners. Resume staging only after the testing results and its own preflight are reviewed.

## Rollback

Suspend the delivery and environment Kustomizations before changing either runtime. Stop all Go
Pods and verify their workers/connections are gone. Restore the recorded Python manifests and
images under the original root owner, with the recorded Service selectors and ports. Reconcile
only the reviewed rollback source and verify retained rows and same-ID recovery before admitting
traffic. The Goose baseline retains the Alembic stamp and existing queue tables; do not downgrade
by clearing them. Recheck schema compatibility before rollback after any later migration.

Never restore an old backup over work admitted after cutover. Runner storage, native conversation
history, retained History and unrelated service databases are outside this deployment handoff.

## Later releases

Update the reviewed serving/migration image pair together when schema changes. A different
migrator digest produces a different Job name using all 256 digest bits; root health and the
serving dependency follow that exact Job. Keep migration execution separate from serving startup.
Restore rolling updates and remove temporary handoff protections only after both environments'
acceptance, without dropping the Dispatcher database or its historical migration fixtures.
