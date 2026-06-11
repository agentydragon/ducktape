# Offline Nodes Block DaemonSet Health Checks

## Problem

The cluster includes roaming nodes (laptops) and a Proxmox CP node that are
frequently offline. DaemonSets (e.g., `prometheus-node-exporter`) schedule pods
on all nodes, but pods on offline nodes stay Pending forever.

Helm's `--wait` blocks until all resources are healthy, including DaemonSets. A
DaemonSet with Pending pods on offline nodes will never satisfy `--wait`,
causing every Helm upgrade to timeout. This blocks Flux reconciliation for the
HelmRelease and everything downstream of it.

### Affected nodes

| Node             | Type         | Availability                    |
| ---------------- | ------------ | ------------------------------- |
| `talos-pve-cp-0` | Talos CP     | Down during Proxmox maintenance |
| `rugged`         | NixOS laptop | Often offline                   |
| `iguana`         | NixOS laptop | Often offline                   |

### Affected HelmReleases

Any HelmRelease that includes a DaemonSet and uses Helm's default `--wait` will
timeout when offline nodes have Pending pods.

| HelmRelease                                     | DaemonSet                  | Desired | Ready                                                   |
| ----------------------------------------------- | -------------------------- | ------- | ------------------------------------------------------- |
| `monitoring/kube-prometheus-stack`              | `prometheus-node-exporter` | 8       | 5                                                       |
| `loki/promtail`                                 | `promtail`                 | 8       | 4                                                       |
| `loki/loki`                                     | `loki-canary`              | 3       | 3 (no tolerations → untainted nodes only, not affected) |
| `node-feature-discovery/node-feature-discovery` | `nfd-worker`               | 8       | 5                                                       |

Cilium DaemonSets (`cilium`, `cilium-envoy`) are also affected (8 desired, 5
ready) but are installed via terraform `null_resource`, not a HelmRelease, so
Flux health checks don't apply.

### Impact (2026-04-06 incident)

`kube-prometheus-stack` HelmRelease timed out repeatedly because
`prometheus-node-exporter` DaemonSet had 3 Pending pods (one per offline node).
This blocked `monitoring-stack` kustomization, which blocked `harbor`, `tempo`,
and 20+ downstream kustomizations. The entire Flux dependency tree stalled for
hours.

## Options

### A. `disableWait` on HelmReleases + explicit Flux health checks

Set `install.disableWait: true` and `upgrade.disableWait: true` on HelmReleases
that include DaemonSets. Move health checking to the Flux kustomization level,
targeting only Deployments/StatefulSets:

```yaml
# flux-kustomization.yaml
healthChecks:
  - apiVersion: apps/v1
    kind: Deployment
    name: grafana
    namespace: monitoring
  - apiVersion: apps/v1
    kind: Deployment
    name: monitoring-operator
    namespace: monitoring
```

**Pro**: Simple, immediate fix. DaemonSets run everywhere including roaming
nodes — monitoring coverage is preserved.

**Con**: Per-HelmRelease configuration. Must maintain the health check list.
Real DaemonSet failures (image pull errors, crash loops on online nodes) are
silently ignored by Helm. Flux health checks partially compensate but don't
cover DaemonSets either.

### B. Auto-remove stale NotReady nodes

Run a controller or CronJob that deletes Node objects after they've been
NotReady for a threshold (e.g., 30 min). The DaemonSet controller stops
targeting deleted nodes, so "all desired pods running" matches reality. When a
node comes back online, kubelet re-registers the Node object.

**Implementation options**:

- Kyverno `CleanupPolicy` on Node objects with NotReady condition
- CronJob: `kubectl get nodes -o json | jq` → delete stale ones
- Dedicated controller (e.g., [draino](https://github.com/planetlabs/draino),
  custom)

**Re-registration behavior** (NixOS workers):

- **Kubelet running when Node deleted**: Re-registers within seconds using
  cached TLS certs at `/var/lib/kubelet/kubelet.conf`. No manual steps.
- **Kubelet restarts after Node deleted**: TLS bootstrap via bootstrap token
  from `secrets/k8s-worker.yaml`. Creates a CSR that needs approval.
- **Labels and taints**: Re-applied by kubelet (`--node-labels`,
  `--register-with-taints` in `k8s-worker.nix`). Kyverno policies re-apply
  dynamic labels (Longhorn disk config, Goldilocks).

**CSR auto-approval**: Talos CCM has `node-csr-approval` enabled but only
approves Talos node CSRs. NixOS worker CSRs require either manual approval or a
separate auto-approver
([kubelet-csr-approver](https://github.com/postfinance/kubelet-csr-approver)).
In practice, re-registration from cached certs (no CSR) covers the common case
of a laptop reconnecting without kubelet restart.

**Pro**: Cleanest solution. Helm and Flux health checks work unchanged. No
per-chart configuration. DaemonSet health accurately reflects reality.

**Con**: Node deletion loses labels/annotations not set by kubelet flags or
Kyverno (unlikely but possible). Needs CSR auto-approval for full automation.
More moving parts.

### C. Taint-based DaemonSet exclusion

Override DaemonSet tolerations to NOT tolerate the
`node-role.kubernetes.io/roaming=true:NoSchedule` taint. DaemonSets only run on
always-on nodes.

**Pro**: Simple, declarative.

**Con**: Loses monitoring on roaming nodes entirely. Doesn't help when
`talos-pve-cp-0` is temporarily NotReady. Rejected — we want node-exporter on
all nodes.

### D. DaemonSet `maxUnavailable` high enough to tolerate offline nodes

Set `updateStrategy.rollingUpdate.maxUnavailable` to the number of
potentially-offline nodes so rollouts don't block.

**Rejected**: The count must be synchronized with the actual number of
offline-capable nodes. Every time a roaming node is added or removed, the count
needs updating across every DaemonSet-bearing HelmRelease. Fragile and annoying
to maintain. Also doesn't fix Helm `--wait` — it still expects all desired pods
to be Ready regardless of `maxUnavailable`.

## Recommendation

**Immediate (option A)**: Add `disableWait` to `kube-prometheus-stack` and any
other HelmReleases with DaemonSets. Unblocks Flux now.

**Long-term (option B)**: Deploy a stale-node cleanup mechanism so the cluster's
node list reflects reality. This makes all health checks work correctly without
special-casing. Requires:

1. Choose implementation (Kyverno CleanupPolicy preferred — already deployed)
2. Set threshold (30 min NotReady → delete)
3. Deploy `kubelet-csr-approver` for NixOS worker CSR auto-approval
4. Test: cordon a node, verify deletion after threshold, verify re-registration
   on reconnect
5. Verify Kyverno re-applies dynamic labels after re-registration
