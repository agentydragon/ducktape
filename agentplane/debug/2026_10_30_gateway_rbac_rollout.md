# Gateway lease loss after RBAC applied ahead of its image

`public-coder-ducktape-2` lost runner connectivity while its Pod remained Ready with
zero container restarts. Exec in central-sampler conversations failed while trying
to resume the retained execution session.

## Evidence

All timestamps below are UTC on 2026-10-31.

- 00:34:32.270084: last renewal of `public-coder-ducktape-2-runner`, epoch 3,
  held by `runner-gateway-74bb68f8fb-gln46`.
- 00:34:37: `kustomize-controller` applied the `runner-gateway` Role without `get`
  on Pods, VirtualMachines and VirtualMachineInstances. The live authorization
  check for `system:serviceaccount:agentplane-staging:runner-gateway` returned
  `no` for `get pods`, `yes` for `list pods`, and `yes` for `get sandboxes`.
- 00:36:02: Sandbox conditions transitioned to `ConnectionLost`.
- 00:38:47: Tempo trace `aff83e778a5c817e6ce7bcc723c079de` records
  `Executor/OpenSession` on `10.244.4.167`: Kubernetes GET 200, GET 403,
  then gRPC `UNAVAILABLE: Sandbox or runner routing is unavailable`.

Both Gateway replicas still ran
`agentplane-runner-gateway:devel-20261030234011-7c9aa1f`. The runner and Codex
executor containers remained running with zero restarts. This failure does not
establish that the native execution session was lost.

## Cause

PR #13134 (`ad853f6b8c`) switched Gateway workload reads to its informer cache
and removed direct-read permissions in the same commit. Flux applied the Role
before an image containing that code was deployed.

The deployed Gateway's `sandboxResolver.destination` reads the Sandbox and then
GETs its Pod. The latter is forbidden; `unavailable` hides the Kubernetes error
behind the generic routing message. Its observer supervisor also GETs Pods;
failed discovery excludes the runner from the wanted set and cancels observation.
Further scans cannot rediscover it, so no heartbeat renews the connection Lease.
This cancellation does not log the usual observer reconnect warning.

## Candidate correction and acceptance

Retain `get/list/watch` on workloads while older Gateway images can serve. These
are namespace-scoped read permissions previously required by those images.

After deployment, verify the Role grants `get pods`, the connection Lease advances,
the Sandbox becomes Ready, and a conversation can execute a harmless command.
Manifest generation alone does not prove recovery. Reverting the permission
restoration while the old image remains deployed recreates the outage.
