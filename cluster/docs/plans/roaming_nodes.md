# Roaming worker follow-ups

[Worker setup and scheduling](../roaming_laptop_worker.md).
[Offline behavior and fencing constraints](../../roaming_node_controller/README.md).

## Validate real reconnects

- **Resume:** At the next laptop resume after the out-of-service taint has landed,
  verify taint removal, Cilium agent and Envoy readiness, and pod networking.
  Both DaemonSets have narrowed tolerations in [Cilium values](../../terraform/main/cilium-values.yaml).
  On 2026-11-14, `rugged` was tainted while offline, untainted 2 seconds after
  kubelet returned, and its DaemonSet pods returned within seconds.
- **Restrictive NAT:** Exercise cross-node pod traffic from `rugged` behind a
  mobile hotspot or restrictive corporate NAT. Verify Nebula relay fallback
  under workload traffic.

## Retire obsolete alert suppressions

After the resume check, review seven days of `ALERTS` before pruning the remaining
null routes in [the Alertmanager config](../../cdk8s/monitoring/stack.py).
`RoamingNodeOffline` inhibits alerts carrying the offline node's label.
Unlabelled pod, DaemonSet, Deployment and Job alerts need their own review,
including pods that stay Pending because a volume pins them to the laptop.

Keep [the Piraeus collector's Ready guard](../../cdk8s/piraeus_metrics.py) during
this cleanup. `KubeVersionMismatch` tracks a separate kubelet version issue.
