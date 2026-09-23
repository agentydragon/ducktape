# Flux ownership changes can disconnect operator-managed Redis Services

## Failure mechanism

During the [Langfuse consolidation (#7454)](https://github.com/agentydragon/ducktape/pull/7454),
Flux adopted the existing Valkey `RedisReplication` under `langfuse`, changing
`kustomize.toolkit.fluxcd.io/name` from `langfuse-cache` to `langfuse`.
The Redis operator propagated that metadata change to Service selectors and the
StatefulSet Pod template. Existing Pods retained the old label until replacement.
A pending replica blocked the rolling update, so the master Service selected no
Pods even though the master was running. Langfuse workers lost cache connectivity.

This was a routing failure during adoption: the custom resource and PVC identities
survived. Identical rendered manifests did not cover Flux-injected labels or
operator-generated resources. `deletionPolicy: Orphan` protects against deletion,
not these reconciliation side effects.

## Upstream behavior

The incident used Opstree Redis operator v0.25.0:

- [RedisReplication construction](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.25.0/internal/k8sutils/redis-replication.go)
  passes CR labels into both Service and StatefulSet construction.
- [Service construction](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.25.0/internal/k8sutils/services.go#L23-L46)
  copies the entire Service metadata label map into `spec.selector`.
- [Issue #1347: Service updated before Statefulset during Reconcilation](https://github.com/OT-CONTAINER-KIT/redis-operator/issues/1347)
  describes Service unavailability after a label change, with a blocked StatefulSet
  update. It was closed; our blocked rollout exposes the same selector mismatch
  through a different reason for the Pods retaining old labels.
- [Merged PR #1382: resolve StatefulSet selector immutability issues](https://github.com/OT-CONTAINER-KIT/redis-operator/pull/1382)
  filters StatefulSet selectors to stable labels but explicitly reverted Service
  selector filtering because Service selectors are mutable. Mutability permits
  the update; it does not keep old Pods reachable during that update.

The durable operator fix is to build Service selectors from stable workload identity
and required role labels, independently of arbitrary metadata labels. Check the
actual operator version's behavior before retiring this warning; the StatefulSet
fix alone does not establish that Service selectors are safe.

## Handoff and guarded recovery

Before changing ownership, compare the CR's labels, each generated Service selector,
the StatefulSet selector and Pod template, and existing Pod labels. Include master,
replica, headless, and metrics Services where present. If selectors contain ownership
labels, plan their transition explicitly; do not assume a rolling update can finish
while a replica is unavailable. Keep the existing Flux pruning/deletion protections.

For the observed post-cutover mismatch, recovery changed only the Flux owner-name
label on the two existing Pods. It did not restart Pods, recreate the StatefulSet,
or alter storage. Use this narrow repair only after establishing the same conditions:

1. Verify the CR has been adopted by the intended Flux owner and the operator has
   reconciled that value into Service selectors and the StatefulSet Pod template.
   Confirm the StatefulSet selector permits the new label value. Ensure the old
   Flux owner cannot reapply the old value.
2. Read each affected Pod fresh. Check its owner reference identifies the expected
   StatefulSet and all other Service selector requirements still match. Capture its
   UID and current owner-name label locally.
3. Patch that one Pod label with a JSON Patch containing `test` operations for both
   `/metadata/uid` and the expected old label before `replace` on
   `/metadata/labels/kustomize.toolkit.fluxcd.io~1name`. If a precondition fails,
   reread and reassess; do not fall back to a broad label overwrite. Do not change
   Redis role labels or replace the whole label map.
4. Verify ready EndpointSlice backends for the affected Services and connect through
   the application's actual Service address from a consuming workload. Check
   application recovery and unchanged Pod UIDs/restart counts and PVC bindings.
   Recheck after operator reconciliation to confirm the repair persists.

In the Langfuse incident, the master endpoint returned, a Redis `PING` from the
worker returned `PONG`, and subsequent worker logs stopped reporting connection
errors. This metadata repair restored connectivity; it did not fix the operator.
Do not encode the obsolete Flux owner name in manifests to preserve routing.
