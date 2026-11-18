# Roaming laptop nodes

Laptops (`iguana`, `rugged`; mesh role `laptop`) are cluster nodes that come and go.

## Goals

- Managed like every other node: same mesh, NixOS kubelet, central logs and metrics while
  online.
- Opportunistic capacity: disposable CI jobs, `rugged`'s local LLM behind LiteLLM,
  strict-local Piraeus volumes.
- A laptop's identity persists while offline. Its Node object stays, so PVCs, PV affinity,
  labels and its Piraeus satellite are remembered and come back with it. Deleting the Node on
  suspend is ruled out.
- Offline is a normal state: no alerts, and no blocked DaemonSet rollout, Helm wait or Flux
  health check.
- A laptop that is online and broken still alerts.

## Problem

Kubernetes has one representation for a Node without a kubelet: NotReady/Unreachable. Every
consumer reads that as a failure. A DaemonSet keeps counting the node as desired. A pod
deleted there stays Terminating and holds the rollout budget. Helm and Flux wait on those
counts, and stock alerts fire. Each consumer has been patched separately:

- A sequential manual Cilium rollout (`cilium.tf`).
- Forked kube-prometheus-stack rules excluding the roaming taint (`monitoring/rules.py`),
  re-diffed every chart bump. Alerts without a `node` label (`KubePodCrashLooping`,
  `KubeDaemonSetRolloutStuck`, `KubeDeployment*`, `KubeJobFailed`, node-exporter
  `TargetDown`) go to the null receiver cluster-wide (`monitoring/stack.py`).
- A hard-coded `only_when_node_ready="rugged"` on the Piraeus `lvm-metrics` collector alert.

The tax grows with laptops × components.

## Design

Give "offline" a real representation that upstream controllers already honor: the
non-graceful node shutdown taint, `node.kubernetes.io/out-of-service`. The
[roaming node controller](../../roaming_node_controller/README.md) adds it after 5 minutes of
kubelet silence and removes it on the first heartbeat. Its README records which upstream
mechanisms react and how. A custom taint was rejected because pod GC force-deletes Terminating
pods only for this key, and without that the DaemonSet deadlock remains.

The static `node-role.kubernetes.io/roaming` taint stays. It keeps ordinary workloads off
laptops and selects which nodes the controller manages.

Rejected alternatives:

- **Medik8s Node Healthcheck plus Self Node Remediation's out-of-service strategy:** its agent
  reboots a node that loses API contact, which is wrong for a laptop on a flaky link.
- **Per-laptop k3s clusters with federation (Liqo, virtual-kubelet):** out of proportion for
  two laptops.

## Remaining work

Verified live on 2026-11-14 with `rugged`: tainted while offline, untainted 2 s after its
kubelet returned, every DaemonSet pod back within seconds.

- **Cilium:** verify on the next laptop resume that its agent and envoy, which no longer
  tolerate the taint, come back and the laptop's pods get networking.
- **Alerts:** node-labelled alerts are inhibited by `RoamingNodeOffline`. Remove the
  null routes for pod, DaemonSet, Deployment and Job alerts in `monitoring/stack.py` after
  the Cilium check above and a review of 7 days of `ALERTS`. The taint lands 5 minutes
  after the kubelet goes silent, so an alert with a shorter `for` still fires in between:
  the Piraeus `lvm-metrics` alerts (2m) keep their `only_when_node_ready` guard.

Out of scope: `KubeVersionMismatch` (NixOS kubelets ahead of Talos) is version drift, not
absence.
