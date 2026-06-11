# PowerDNS Operator: Stuck Failed ClusterRRsets

**Date**: 2026-04-07
**Status**: Mitigated (structural fix applied, upstream bug not yet filed)

## Symptoms

- `api.allegedly.works` did not resolve — DNS record missing from PowerDNS
- All 8 `ClusterRRset` resources showed `syncStatus: Failed` since bootstrap
  (`2026-04-03T19:44:50Z`)
- Operator logs showed no errors — only "Reconcile ClusterRRset" with no
  success/failure follow-up
- `ClusterZone` showed `syncStatus: Succeeded` — zone itself was healthy
- External-dns-managed records (`*.allegedly.works` from HTTPRoutes) worked
  fine since they bypass the operator entirely

## Root Cause

Two compounding issues:

### 1. Bootstrap Race Condition

The `ClusterZone` and all `ClusterRRset` resources were in the **same Flux
Kustomization** (`powerdns-zones`), applied simultaneously. During bootstrap:

1. Flux applies ClusterZone + all ClusterRRsets at the same time
2. ClusterRRset controller reconciles before ClusterZone finishes syncing
   to PowerDNS
3. ClusterRRset sees zone with `syncStatus != Succeeded` (or zone doesn't
   exist in PowerDNS yet)
4. Controller sets `syncStatus=Failed` with reason "ZoneNotAvailable" —
   **no `log.Error()` call**, completely silent

### 2. Operator "Stuck Failed" Design Bug

In `internal/controller/common.go` lines 227-231 of powerdns-operator v0.6.0:

```go
if isInFailedStatus && !isModified {
    updateRrsetsMetrics(getRRsetName(gr), gr)
    return ctrl.Result{}, nil
}
```

Once a ClusterRRset reaches `syncStatus=Failed` and its spec hasn't changed
(same `generation`), **the controller never retries**. It short-circuits on
every subsequent reconcile with no log output and no requeue. The zone
eventually succeeds, but all RRsets are permanently stuck.

Additionally, at `common.go:322-327`, the final status patch drops the
`Conditions` field — the error message explaining why the sync failed is
lost even when looking at the resource status.

## Impact

- `api.allegedly.works` (k8s API endpoint) didn't resolve — broke
  kubeconfig for Claude Code web sessions and any external kubectl access
- NS glue records (`ns1`, `ns2`) were also missing — could have caused
  full zone delegation failure if external-dns hadn't independently created
  the apex records
- Nebula lighthouse A records missing — new nodes joining the mesh would
  fail to find lighthouses via DNS (mitigated by direct IP in nebula config)

## Fix

### Immediate (manual unstick)

Bump `spec.ttl` on each ClusterRRset to change `generation`, bypassing the
stuck-failed guard:

```bash
for name in $(kubectl get clusterrrset -o jsonpath='{.items[*].metadata.name}'); do
  ttl=$(kubectl get clusterrrset "$name" -o jsonpath='{.spec.ttl}')
  kubectl patch clusterrrset "$name" --type=merge -p "{\"spec\":{\"ttl\":$((ttl + 1))}}"
  kubectl patch clusterrrset "$name" --type=merge -p "{\"spec\":{\"ttl\":$ttl}}"
done
```

All 8 RRsets synced successfully after this.

### Structural (prevent recurrence)

Split `powerdns-zones` Flux Kustomization into two:

- **`powerdns-zone`**: ClusterZone only (`zones/allegedly.works/`)
- **`powerdns-records`**: All ClusterRRsets (`zones/allegedly.works/records/`),
  with `dependsOn: powerdns-zone`

This ensures the zone is fully synced in PowerDNS before any RRsets are
applied.

## Diagnosis

```bash
# Check sync status of all RRsets
kubectl get clusterrrset -o custom-columns='NAME:.metadata.name,STATUS:.status.syncStatus'

# Operator logs show reconcile but no success/failure
kubectl logs -n powerdns-operator -l app.kubernetes.io/name=powerdns-operator

# Verify PowerDNS API is reachable and authenticated
kubectl exec -n dns-system <powerdns-pod> -- pdnsutil list-zone allegedly.works

# Unstick by bumping generation (TTL toggle)
kubectl patch clusterrrset <name> --type=merge -p '{"spec":{"ttl":301}}'
kubectl patch clusterrrset <name> --type=merge -p '{"spec":{"ttl":300}}'
```

## TODO

- [ ] File upstream bug: powerdns-operator "stuck Failed" — once a
      ClusterRRset reaches Failed status, it never retries unless spec
      changes. Should retry with backoff, or at minimum log a warning.
      Repo: `https://github.com/powerdns-operator/PowerDNS-Operator`
