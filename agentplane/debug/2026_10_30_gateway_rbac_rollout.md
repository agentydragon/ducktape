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

## Thread abort: 2463a826-67af-4423-82ec-bd977a731470

The sampler database confirms intent `a9f429d8-ee93-4ef6-ac42-250601471adb`
belongs to this Session (conversation `32ff0d99-7e6c-4cff-822a-bccf00c99a76`).
At 00:41:25.116 it recorded a fresh `initialize` request with
`resume_session_id: null`. Trace `1e3c7a171cd63fc46bf076681f5820de`, starting
00:41:25.164837, shows the same GET 200, GET 403, then routing-unavailable error.
Gateway `OpenSession` returns from `executionConnection` before calling the
private Runner, so this initialization did not reach the native executor.

`Executor.openSession` classifies every initial stream receive error as an
`executorTransportError` and records the outcome as unknown. The error escapes
`Runtime.connectHost` before the requested model tool runs. `Store.settleFailure`
blocks any failure not wrapped in `KnownFailureError`, independently of whether
there are unresolved effects. Event 21702 records the block at 00:41:25.330535;
event 21703 records `TURN_STATUS_FAILED` at 00:41:25.344426. The Session is stopped,
blocked, and has no active turn.

At inspection there were zero unresolved intents, all 25 cells were completed,
and the 55 recorded processes were exited (25) or abandoned (30). These counts
do not prove historical abandoned processes had no effects; they establish that
the generic blocking message did not identify a currently unresolved intent or
live execution handle. The retained executor-session ID in the error evidence
was historical: the failed request itself asked to open a fresh session.

Restoring routing cannot clear this durable sampler block. A separate correction
should carry explicit pre-dispatch rejection evidence across the Gateway boundary
and classify it without inferring dispatch from a generic gRPC status. Generic
transport loss must continue to preserve uncertainty about actual remote effects.

## Candidate correction and acceptance

Retain `get/list/watch` on workloads while older Gateway images can serve. These
are namespace-scoped read permissions previously required by those images.

After deployment, verify the Role grants `get pods`, the connection Lease advances,
the Sandbox becomes Ready, and a conversation can execute a harmless command.
Manifest generation alone does not prove recovery. Reverting the permission
restoration while the old image remains deployed recreates the outage.
