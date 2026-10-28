# Offline Nodes Block DaemonSet Health Checks

> **Source state (2026-10):** upgrade `disableWait` is configured for
> `kube-prometheus-stack`, `promtail`, and Node Feature Discovery. The two
> `promtail` releases and Node Feature Discovery also bound rolling updates above
> the roaming-node count; `test_roaming_daemonset_capacity` checks that bound
> against `nebula-mesh.json`. These settings address Helm upgrade waits and
> rollout capacity. The stale-Node cleanup proposal in Option B remains open.

## Problem

The cluster includes roaming nodes (laptops) that are frequently offline.
DaemonSets schedule pods on those nodes, but pods cannot become Ready while a
node is absent.

Helm's `--wait` blocks until all resources are healthy, including DaemonSets. A
DaemonSet with Pending pods on offline nodes will never satisfy `--wait`,
causing some Helm upgrades to time out and blocking dependent Flux
reconciliation. The releases that need this treatment change over time; inspect
their current chart and rendered resources before applying the options below.

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

**Con**: Loses monitoring on roaming nodes entirely. Doesn't help when an
always-on node is temporarily NotReady. Rejected — we want node-exporter on all
nodes.

### D. DaemonSet `maxUnavailable` high enough to tolerate offline nodes

Set `updateStrategy.rollingUpdate.maxUnavailable` above the number of
potentially-offline nodes so a rollout can progress while those nodes are absent.
This is a rollout-capacity guard, not a replacement for Helm's wait policy:
`maxUnavailable` alone does not make Helm stop waiting for every DaemonSet pod.

The current source uses this with `upgrade.disableWait` for `promtail`,
`promtail-journal`, and Node Feature Discovery. The
[roaming DaemonSet check](../../cdk8s/monitoring/test_roaming_daemonset_capacity.py)
derives the laptop count from `nebula-mesh.json` and verifies that each configured
budget exceeds it. Kube-prometheus-stack uses `disableWait` for its install and
upgrade without this additional budget.

## Recommendation

**Current source (option A)**: `kube-prometheus-stack`, `promtail`, and Node
Feature Discovery set `upgrade.disableWait`; the two `promtail` releases and Node
Feature Discovery also use the checked rolling-update budget described in
Option D. Inspect each chart before applying this to another release.

**Long-term (option B)**: Deploy a stale-node cleanup mechanism so the cluster's
node list reflects reality. This makes all health checks work correctly without
special-casing. Requires:

1. Choose implementation (Kyverno CleanupPolicy preferred — already deployed)
2. Set threshold (30 min NotReady → delete)
3. Deploy `kubelet-csr-approver` for NixOS worker CSR auto-approval
4. Test: cordon a node, verify deletion after threshold, verify re-registration
   on reconnect
5. Verify Kyverno re-applies dynamic labels after re-registration
