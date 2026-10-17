# Go delivery rollout

The environment root owns CNPG, the Dispatcher database/Secret, both stable Services, and the
Goose Job. A separate `agentplane-{testing,staging}-delivery` Flux Kustomization owns the Go
workload, its account, RBAC and policies. The serving dependency checks the root's current
generation, Ready condition, suspension state and that the root health-checks the migration Job.
Flux image automation advances the Gateway and migrator images like every other service
([Later releases](#later-releases)).

The activation configuration releases both environment roots. Merge staging activation only after
testing acceptance, staging's backup/restore preflight and writer fence, and verification of the
staged artifacts. Until that activation merges, staging remains suspended in deployed source.
Release suspension in reviewed source, rather than relying on an imperative resume that GitOps can
overwrite.

Testing is disposable: the operator waived its preservation and manual writer-fence gates. This
exception does not apply to staging or establish testing acceptance.

## Preparation

1. Merge and reconcile the pruning-protection change before changing ownership. Inspect the
   live Gateway account, RBAC, policies and staging PDB for
   `kustomize.toolkit.fluxcd.io/prune: disabled`. The two Services and database keep their root
   owner throughout. Record their UIDs and the root's applied artifact revision.
2. Before merging the activation change, merge the staged rollout configuration. Confirm both roots are suspended and each delivery
   Kustomization is blocked by its root. Inspect each root and serving ExternalArtifact to ensure
   the staged manifests and pinned images are available before releasing suspension.
3. Confirm the `agentplane-runner-gateway-go` and `agentplane-runner-gateway-queue-migrate` entries in
   the environment's `image-pins` name published tags.
4. Follow [state preservation](staging_data_preservation.md): capture the existing Deployment
   manifests/images, resource identities, queue/opening identities and states, and backup/restore
   evidence. Verify the Dispatcher database's sole Alembic head is
   `0007_drop_opening_attached`. The Goose preflight also verifies columns and indexes and refuses
   a different baseline. Do not log payloads, tokens or credentials.

The schema handoff's automated proof is
`//agentplane/runner_gateway/migrations:test_migrations`, including retained queued work and
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
   the new root artifact. Its Goose Job must complete; the serving dependency
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

Flux rolls the migrator and the Gateway independently, so neither image can assume the other is
already running the matching release. A schema change therefore lands in order, each step merged
only after the previous one is running in both environments:

1. **Migration first.** Add the Goose migration alone, written so Gateway builds from before and
   after it both work against the old and the new schema. Flux picks up the migrator and
   force-recreates the root-owned Job; wait for it to complete and let it soak.
2. **Code that uses it afterwards.** Merge the Gateway change that depends on the new schema only
   once step 1 has soaked.
3. **Removals go the other way.** Stop the Gateway using a column or table first; the migration that
   drops it lands last.

Every Gateway image change restarts the Deployment with `Recreate` until rolling updates are
restored, so delivery drops briefly each time. Keep migration execution separate from serving
startup. Restore rolling updates and remove temporary handoff protections only after both
environments' acceptance, without dropping the Dispatcher database or its historical migration
fixtures.
