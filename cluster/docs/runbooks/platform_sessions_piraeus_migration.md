# Platform Sessions: SeaweedFS to Piraeus

Provider session PVCs are migrated one at a time or in reviewed batches. Captures have
separate Piraeus PVCs and are not copied here. The local capture PVCs still pin providers
to their current nodes.

The `Recreate` Deployment stops the old provider before its replacement starts. Its
first init container reads the old PVC read-only and atomically publishes a verified copy
to the new replicated Piraeus PVC. Subsequent starts use the published copy and never
replace it from the old PVC. Both PVCs are kept; the old SeaweedFS PVC becomes stale as
soon as the provider writes to Piraeus. Reverting the Deployment to the old PVC would
lose subsequent updates: repair forward on the Piraeus PVC instead.

After each rollout, check the new PVC is bound, the migration init container succeeded,
the provider and recorder are ready, and the gateway reports the expected session state.
The session-state response is a local snapshot, not proof of fresh upstream authentication.
Blue Shield already required reauthentication before the canary migration.

```sh
kubectl -n platform-sessions get pvc platform-sessions-blueshield-piraeus
kubectl -n platform-sessions rollout status deployment/platform-sessions-blueshield
kubectl -n platform-sessions get pods -l app=platform-sessions-blueshield -o jsonpath='{.items[0].status.initContainerStatuses[?(@.name=="migrate-state")].state.terminated.exitCode}{"\n"}'
```

Roll the remaining providers only after the canary is verified. Once every migrated
provider is verified, remove the migration init container and its old SeaweedFS mount
from the Deployment so a restart no longer depends on SeaweedFS. Keep the old PVCs and
Piraeus `state` subdirectory; cleanup of old state requires a separate reviewed decision.
