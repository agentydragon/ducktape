# RWO Volume + RollingUpdate Deadlock

**Date**: 2025-11 (exact date unknown)
**Status**: Resolved

## Symptoms

Pod stuck in `Init:Error` or similar, new pod stuck in `Pending` with `Multi-Attach error`.

## Root Cause

Single-replica Deployments with RWO (ReadWriteOnce) volumes using default `RollingUpdate`
strategy create deadlocks. RollingUpdate starts new pod before terminating old one, but RWO volumes
can only attach to one node. Old pod won't release volume until new pod is Ready, new pod can't
become Ready without volume.

## Solution

Use `strategy.type: Recreate` for single-replica deployments with RWO volumes:

```yaml
spec:
  replicas: 1
  strategy:
    type: Recreate # Terminates old pod before creating new one
```

**When to use Recreate**: Single replica + RWO volume + stateful app (databases, git servers, registries).
Brief downtime during updates is acceptable tradeoff vs deadlocks requiring manual intervention.

## Audit Command

Find affected deployments:

```bash
for ns in $(kubectl get ns -o jsonpath='{.items[*].metadata.name}'); do
  kubectl get deployment -n $ns -o json | jq -r '
    .items[] | select(.spec.replicas == 1 and .spec.strategy.type == "RollingUpdate") |
    select(.spec.template.spec.volumes[]?.persistentVolumeClaim != null) |
    "\(.metadata.namespace)/\(.metadata.name)"'
done
```
