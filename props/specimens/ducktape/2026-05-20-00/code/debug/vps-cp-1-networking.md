# talos-vps-cp-1: Broken Pod Networking (Stale podCIDR)

**Date**: 2026-03-19
**Status**: Active — blocking 84 Flux kustomizations, 44 pods Pending

## Summary

`talos-vps-cp-1` has pods with IPs from an old podCIDR (`10.244.4.0/24`) that
no longer matches the node's current podCIDR (`10.244.2.0/24`). Cilium was
restarted during a Terraform-driven node upgrade and only routes the new CIDR.
The stale pods can't reach ClusterIP services (`10.96.0.1:443` → "no route to
host"), which breaks Longhorn, Vault, and the entire external-secrets dependency
chain.

## Root Cause

During a Terraform infrastructure apply, VPS nodes were recreated/upgraded and
temporarily picked up transient hostnames (`talos-34f-5sc`, `talos-ner-5do`).
When `talos-vps-cp-1` came back:

1. **podCIDR changed** from `10.244.4.0/24` to `10.244.2.0/24` (Kubernetes
   assigned a new CIDR to what it saw as a new node)
2. **Cilium was restarted** (started 2026-03-19T09:42:46Z) and now only
   programs eBPF routes for the new `10.244.2.0/24` CIDR
3. **Some DaemonSet pods survived** from before the upgrade and still hold
   `10.244.4.x` IPs that Cilium doesn't route

## Affected Pods (stale 10.244.4.x IPs)

| Namespace                | Pod                                   | IP             | Started              |
| ------------------------ | ------------------------------------- | -------------- | -------------------- |
| `longhorn-system`        | `longhorn-manager-p8mqp`              | `10.244.4.38`  | 2026-03-08T08:09:36Z |
| `longhorn-system`        | `engine-image-ei-ff1cedad-wzdnx`      | `10.244.4.191` | 2026-03-08T08:10:16Z |
| `loki`                   | `loki-stack-promtail-pm466`           | `10.244.4.60`  | 2026-03-08T07:20:24Z |
| `kube-system`            | `hcloud-csi-node-f2m62`               | `10.244.4.230` | 2026-03-19T09:42:46Z |
| `node-feature-discovery` | `node-feature-discovery-worker-68kf8` | `10.244.4.210` | 2026-03-19T09:42:46Z |

The critical one is `longhorn-manager-p8mqp` — it's been running since March 8
but can no longer reach the API server, making it `Ready: False` (1/2
containers).

## Blast Radius

The cascade from the stale longhorn-manager pod:

1. **Longhorn manager not ready on `vps-cp-1`** → Longhorn marks node as
   `NotReady` (condition: `ManagerPodDown`)
2. **Longhorn can't attach volumes** on `vps-cp-1` → Vault PVCs fail to attach:
   `"node talos-vps-cp-1 is not ready, couldn't attach volume ... to it"`
3. **Vault pods stuck in `Init:0/1`** — both `instance-0` and `instance-1`
   waiting for volume attach
4. **`vault-backend` ClusterSecretStore → `InvalidProviderConfig`** — can't
   reach Vault at `instance.vault:8200`
5. **`external-secrets-config` health check fails** — depends on all
   ClusterSecretStores being healthy
6. **84 kustomizations blocked** — most depend transitively on
   `external-secrets-config`, `harbor-pull-secret`, `monitoring-stack`, or
   `vault-token`
7. **44 pods Pending** — volume attachment failures + harbor down
   (ImagePullBackOff for internal registry images)

### Additional issues on `vps-cp-1`

- `hcloud-csi-node-f2m62` in `CrashLoopBackOff` (116 restarts) — can't reach
  Hetzner metadata service at `169.254.169.254` either (also "no route to
  host"), likely same stale-CIDR networking issue

## Stale Longhorn Node Entries

Longhorn still tracks nodes from the transient TF hostnames:

| Longhorn Node    | Ready | Schedulable | Notes                                          |
| ---------------- | ----- | ----------- | ---------------------------------------------- |
| `talos-34f-5sc`  | False | True        | Stale — was `talos-vps-cp-1` during TF upgrade |
| `talos-ner-5do`  | False | True        | Stale — was another VPS node during TF upgrade |
| `talos-vps-cp-1` | False | True        | Current node — `ManagerPodDown`                |
| `wyrm2`          | False | False       | Known down — not in cluster                    |

These stale entries hold replicas that should be cleaned up or rebuilt
elsewhere.

## Suggested Fix

### Immediate: Restart stale pods

Delete the pods with `10.244.4.x` IPs so their DaemonSets/controllers recreate
them with IPs from the current `10.244.2.0/24` CIDR:

```bash
# The critical one — will fix Longhorn → Vault → everything
kubectl delete pod longhorn-manager-p8mqp -n longhorn-system

# Other stale pods
kubectl delete pod engine-image-ei-ff1cedad-wzdnx -n longhorn-system
kubectl delete pod loki-stack-promtail-pm466 -n loki
kubectl delete pod hcloud-csi-node-f2m62 -n kube-system
kubectl delete pod node-feature-discovery-worker-68kf8 -n node-feature-discovery
```

After longhorn-manager comes back healthy, Vault volumes should attach and the
cascade should self-heal.

### Cleanup: Remove stale Longhorn nodes

Once the cluster stabilizes:

```bash
kubectl delete node.longhorn.io talos-34f-5sc -n longhorn-system
kubectl delete node.longhorn.io talos-ner-5do -n longhorn-system
```

**Caution**: Check that no volumes have their only healthy replica on these
stale nodes before deleting. Replicas on stale nodes should be rebuilt on live
nodes first.

### Verify: After pod restarts

```bash
# Longhorn manager should be 2/2 Ready
kubectl get pods -n longhorn-system -l app=longhorn-manager -o wide

# Longhorn node should flip to Ready
kubectl get nodes.longhorn.io -n longhorn-system

# Vault pods should start attaching volumes
kubectl get pods -n vault

# external-secrets-config should reconcile
kubectl get kustomization external-secrets-config -n flux-system
```

## Cluster Resource Context

With wyrm2 down, the cluster has only 3 nodes:

| Node             | Role          | CPU usage | Memory usage | Memory requests |
| ---------------- | ------------- | --------- | ------------ | --------------- |
| `talos-pve-cp-0` | control-plane | 9%        | 45%          | 19%             |
| `talos-vps-cp-0` | control-plane | 41%       | 84%          | 76%             |
| `talos-vps-cp-1` | control-plane | 90%       | 79%          | 84%             |

VPS nodes are heavily loaded. Even after fixing networking, some pods may remain
Pending due to memory pressure until wyrm2 returns.
