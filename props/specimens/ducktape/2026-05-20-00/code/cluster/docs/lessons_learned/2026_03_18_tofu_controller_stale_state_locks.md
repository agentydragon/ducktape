# tofu-controller Stale State Locks After Controller Restart

**Date**: 2026-03-18
**Status**: Unresolved (stale locks require manual cleanup)

## Root Cause

When the tofu-controller deployment is restarted (via `kubectl rollout restart` or any
pod template change), runner pods that are mid-plan hold terraform state locks stored as
`coordination.k8s.io/v1 Lease` objects. These locks are never released because:

1. **Runner pods have no `ownerReferences`** — they are standalone pods, not owned by
   the controller deployment. They survive controller restarts as orphans.
2. **TLS cache desync** (see <2025_11_19_tofu_controller_tls_cache_desync.md>): the new
   controller pod runs startup GC, deletes all TLS secrets, and regenerates new ones.
   Orphaned runners from the old controller have stale TLS certs — gRPC communication
   between new controller and old runners fails.
3. **No gRPC = no lock release**: lock release happens via the controller's gRPC call
   chain (runner reports success → controller tells runner to release → runner releases
   lock → controller deletes runner). With TLS mismatch, this chain never executes.
4. **tofu-controller has no stale lock detection**: it never checks whether the
   lock-holding pod (identified by `Who: runner@xxx-tf-runner` in the Lease annotation)
   still exists. It never invokes `tofu force-unlock`.

The result is a permanent deadlock: new runners can't acquire the lock, the controller
keeps retrying every 15s, and the lock persists until manually cleared.

## Incident Timeline (2026-03-12 → 2026-03-18)

Three `kubectl rollout restart` commands were issued against the tofu-controller:

| Time (PDT)   | Event                                                      |
| ------------ | ---------------------------------------------------------- |
| Mar 8 00:19  | Bootstrap: tofu-controller deployed (RS rev 1)             |
| Mar 9 07:08  | `kubectl rollout restart` → RS rev 2                       |
| Mar 10 16:20 | `kubectl rollout restart` → RS rev 3                       |
| Mar 12 16:00 | `kubectl rollout restart` → RS rev 4 (current)             |
| Mar 12 16:36 | All 24 `lock-tfstate-*` Lease objects created              |
| Mar 13–17    | Runners stagger lock acquisition (15m reconcile intervals) |
| Mar 18       | 21 of 24 Terraform resources stuck on stale locks          |

The restart on Mar 12 killed the rev 3 controller pod while runners were in-flight.
Orphaned runners held locks, died without releasing them, and the new controller's
runners have been failing against those locks for 5+ days.

### Why 3 of 24 succeeded

- `alloy-otlp-bearer-token`, `ollama-direct-token`: created after the last restart (no
  prior lock)
- `harbor-proxy-cache`: runner completed fast enough to release its lock before the
  restart killed anything

## Affected Resources

21 Terraform resources stuck with `error acquiring the state lock`. These block a
cascade of ~60 Flux Kustomizations that depend on secrets managed by these Terraform
modules (Authentik, Harbor, Gitea, Ollama, PowerDNS, etc.).

## Lock Mechanism

The terraform `kubernetes` backend stores locks as Lease objects in the same namespace
as the tfstate secrets:

```yaml
Lease: lock-tfstate-default-{name}
  namespace: flux-system
  spec.holderIdentity: <lock-uuid>
  annotations:
    app.terraform.io/lock-info: {"ID":"...","Operation":"OperationTypePlan",
      "Who":"runner@{name}-tf-runner","Created":"..."}
```

A lock is held when `spec.holderIdentity` is non-empty. Releasing the lock clears
`holderIdentity` (the Lease object itself persists).

## Diagnosis

```bash
# List all stale locks (Leases with holderIdentity set)
kubectl get leases -n flux-system -o json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data['items']:
    name = item['metadata']['name']
    if not name.startswith('lock-tfstate-'):
        continue
    holder = item['spec'].get('holderIdentity', '')
    if holder:
        ann = item['metadata'].get('annotations', {})
        info = ann.get('app.terraform.io/lock-info', '')
        short = name.replace('lock-tfstate-default-', '')
        print(f'LOCKED: {short:35s} holder={holder}')
"

# Verify the lock-holding pod no longer exists
kubectl get pods -n flux-system -l app.kubernetes.io/name=tf-runner --no-headers
# Current runners are NEW pods that fail against the stale lock — not the original holders
```

## Resolution

Delete the stale lock Leases. The underlying tfstate secrets and Vault data are
unaffected — the runners just need to successfully run `tofu plan` once.

```bash
# Option 1: Delete all lock Leases (safest — Leases are recreated on next lock)
kubectl delete leases -n flux-system -l tfstate=true

# Option 2: Clear holderIdentity on specific Leases
for lease in $(kubectl get leases -n flux-system -o json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data['items']:
    name = item['metadata']['name']
    holder = item['spec'].get('holderIdentity', '')
    if name.startswith('lock-tfstate-') and holder:
        print(name)
"); do
  kubectl patch lease "$lease" -n flux-system --type=merge \
    -p '{"spec":{"holderIdentity":null}}'
done
```

After clearing locks, force-reconcile all Terraform resources:

```bash
kubectl annotate terraform -n flux-system --all \
  reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
```

## Prevention

### Do not `kubectl rollout restart` the tofu-controller

The restart is the trigger. If a restart is genuinely needed:

1. Suspend all Terraform resources first
2. Delete all runner pods
3. Restart the controller
4. Wait for it to become ready
5. Unsuspend Terraform resources

```bash
# Safe restart procedure
kubectl get terraform -n flux-system -o name | \
  xargs -I {} kubectl patch {} -p '{"spec":{"suspend":true}}' --type=merge
kubectl delete pods -n flux-system -l app.kubernetes.io/name=tf-runner --wait=true
kubectl rollout restart deployment/tofu-controller -n flux-system
kubectl rollout status deployment/tofu-controller -n flux-system --timeout=60s
kubectl get terraform -n flux-system -o name | \
  xargs -I {} kubectl patch {} -p '{"spec":{"suspend":false}}' --type=merge
```

### Upstream bug: no stale lock cleanup

tofu-controller should detect stale locks by checking whether the lock-holding pod
still exists. If `Who: runner@xxx-tf-runner` references a pod that no longer exists,
the controller should force-unlock (delete the Lease `holderIdentity`). This is a
straightforward check: parse the `Who` field, query the pod, force-unlock if not found.

Related upstream issue: the controller also lacks `ownerReferences` on runner pods,
which would let Kubernetes garbage-collect orphaned runners when the controller pod
dies. Adding owner references would not fix the lock problem directly (the lock is in
a Lease, not the pod), but would prevent orphaned runners from interfering.

## Key Lessons

1. **Terraform state locks via Kubernetes Leases have no TTL** — unlike etcd leases or
   database advisory locks, K8s Lease objects persist until explicitly cleared. There is
   no automatic expiration.
2. **tofu-controller has no force-unlock mechanism** — it doesn't check if the lock
   holder pod still exists, doesn't invoke `tofu force-unlock`, and doesn't attempt to
   clear stale Leases. The `force: true` field on the Terraform CRD forces re-plan/apply
   but does not force-unlock.
3. **Runner pods are fire-and-forget** — no `ownerReferences`, no finalizers, no cleanup
   hooks. If the controller loses track of a runner, the runner's side effects (locks)
   persist indefinitely.
4. **Controller restarts are not safe** — the combination of TLS cache desync (breaks
   gRPC to old runners) and no stale lock detection makes any controller restart
   potentially destructive. Three restarts in 4 days caused 5+ days of broken GitOps.
5. **The restartedAt annotation is the smoking gun** — `kubectl rollout restart` writes
   `kubectl.kubernetes.io/restartedAt` to the pod template, creating a new ReplicaSet
   even though nothing else changed. Check RS annotations to identify restart-induced
   rollouts.
