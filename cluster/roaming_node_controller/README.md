# Roaming node controller

Marks a roaming laptop node (one tainted `node-role.kubernetes.io/roaming=true:NoSchedule`)
as out of service while its kubelet is silent, so the cluster treats an offline laptop as
gone instead of failing, while keeping its Node object and everything bound to it.

## Behavior

- **Taint:** a roaming node whose kubelet Lease (`kube-node-lease/<node>`) has not been renewed
  for 5 minutes, and that is not `Ready`, gets
  `node.kubernetes.io/out-of-service=nodeshutdown:NoExecute`.
- **Untaint:** on the first Lease renewal after that (a renewal younger than the Lease's
  `leaseDurationSeconds`). The controller watches Leases, so this follows the renewal within
  about a second; the kubelet renews every 10 seconds.
- Nodes without the roaming taint are never touched. Taints are patched with an optimistic
  lock, so the node lifecycle controller's concurrent taint edits are never overwritten.

The 5-minute delay outlasts a suspend/resume or a Nebula re-handshake; the immediate untaint
plus delayed taint bound how often a flapping link can evict pods.

## What the taint does

Upstream Kubernetes (checked in the 1.35 source) reacts to the taint as follows:

- **DaemonSets** that do not tolerate it drop the node from `desiredNumberScheduled` and the
  other status counts, and delete their pod there. Helm's `--wait`, Flux's kstatus health
  check and `KubeDaemonSetRolloutStuck` all compare those counts, so they see a finished
  rollout.
- **Pod GC** force-deletes Terminating pods on a node that is NotReady and carries this
  taint key (only this key). Without that, a pod deleted on an offline node stays
  Terminating forever. The DaemonSet rolling update counts it as unavailable, which is the
  deadlock in
  [the promtail incident](../docs/lessons_learned/2026_07_31_promtail_daemonset_roaming_deadlock.md).
- **Taint eviction** deletes every other pod there that does not tolerate the taint. The
  attach-detach controller force-detaches the node's volumes.

The Node object, its labels, PVCs and PV node affinity are untouched. A pod pinned to a
laptop-local volume is recreated Pending and schedules back when the laptop returns.

**Gotcha:** a DaemonSet that tolerates every taint (`- operator: Exists` with no key and no
effect, Cilium's default) still counts an offline laptop and still blocks on it.

**Gotcha:** upstream documents this taint for nodes that are actually shut down. A laptop
that is running but cut off from the API server for 5 minutes has its pods deleted. Its
kubelet kills them when it reconnects. That is safe for DaemonSets and node-local storage. A
writer to replicated storage placed on a laptop would need fencing first.

## Running

One replica on the OVH nodes, `Recreate` strategy, no leader election. Deployment:
`cluster/cdk8s/roaming_node_controller.py`. Tests: `bbr test //cluster/roaming_node_controller/...`.
