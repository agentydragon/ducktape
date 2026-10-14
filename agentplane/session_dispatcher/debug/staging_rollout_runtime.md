# Staging Dispatcher rollout: exited init container still reported running

Read-only investigation on 2026-10-14, approximately 21:30–21:34 UTC. This is a runtime
reconciliation failure observed on `ovh-ns102453` (`10.42.0.15`), not evidence of a database
migration lock. Its deeper containerd cause and recovery remain unresolved. No Pod deletion,
service restart, node change or database write was performed.

## Evidence

- Testing has a Ready Dispatcher on `devel-20261014211201-31e0b37`, with migration image
  `devel-20261014211153-31e0b37` successfully exited. Staging has one Ready new replica and one
  Ready old `a3161b0` replica. Deployment `readyReplicas: 2` therefore does not prove full rollout.
- Staging Pod `agentplane-session-dispatcher-6fdfb5ffd-q5lg6` reports its `migrate` init container
  running since 21:21:29, with restart count zero. Its application container remains waiting.
- Containerd's CRI log records that same container starting at 21:21:29.873 and receiving its
  exit event at 21:21:59.349. Container ID:
  `556d49fdee86cc10f4b0bc1ccd17b41146c494153527eb5e159d73812fcaf4bb`.
- A read-only exec attempt at 21:30 returned `no running task found` for that exact container.
  Its logs are empty. PostgreSQL activity has no connection from its Pod IP `10.244.4.30`, and
  no blocking PIDs for the current Dispatcher connections.
- The same node's kubelet repeatedly reports `detected running containers after a successful
KillPod, CRI violation` for old app and Gateway Pods. Notification Pod creation also fails
  because the sandbox name remains reserved. The node reports Ready and no resource pressure;
  its runtime is containerd 2.3.4 on Talos 1.14.0.
- An earlier container's TaskExit handling failed to delete its task with a deadline error at
  21:21:08, then logged successful cleanup at 21:21:10. That precedes the stuck init container,
  but is not sufficient to establish the underlying defect or choose a recovery operation.

## Gate and next diagnosis

Testing can exercise the new receiver. Keep staging wake activation gated until every serving
Dispatcher replica has the new receiver and the Deployment has fully reconciled. Preserve the
runtime evidence before any operator-approved recovery; do not mask this by lengthening migration
timeouts or changing application start order.

Read kubelet/CRI logs directly through the configured Talos API (`talosctl -e 10.42.0.15
-n 10.42.0.15 logs kubelet` / `logs cri`). The default endpoint refused request forwarding, so the
explicit node endpoint is required. The Kubernetes `nodes/proxy` permission was not probed.
