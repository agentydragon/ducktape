# Lessons learned

Durable lessons from resolved Agentplane incidents. The full investigations are in git history;
each entry names the fix commit.

## Narrow RBAC only after the image that no longer needs it serves

2026-10-31 staging: PR #13134 moved Runner Gateway's workload reads to its informer cache and
dropped `get` on Pods, VirtualMachines and VirtualMachineInstances in the same commit. Flux applied
the Role before the new image rolled out. The old Gateway's per-request Pod `get` returned 403, which
it reported as the generic `Sandbox or runner routing is unavailable`; its observer stopped
rediscovering the runner, the connection Lease expired, and Sandboxes went `ConnectionLost` while
their Pods stayed Ready. #13176 restored the reads.

Flux reconciles RBAC and Deployments independently. Remove a permission in a later change, once no
serving image needs it. A 403 behind a generic routing or availability error is worth checking first
after an RBAC change.

## Keep a sidecar that carries the workload's telemetry alive past the workload

2026-10-31: kubelet sent SIGTERM to Runner and its egress sidecar together. The sidecar hosts
Runner's OTLP route and exited within 50 ms, so Runner's final shutdown spans had no export path and
Runner lingered about five seconds before exiting. #13319 made the relay a native sidecar
(`initContainers` with `restartPolicy: Always`, the `keep_sidecar_until_workload_exit` option in
`cluster/cdk8s/agentplane/sandbox_pod.py`), which kubelet stops after the workload containers.

## `fsGroup` can widen a persistent directory's mode

2026-10-31: after Pod resume, Execution Service refused its persistent `/state/execution` directory
because it had group or other permission bits (`execution state directory must be private (0700)`),
and Runner Gateway stayed `ConnectionPending`. The Pod uses `fsGroup: 1000`; kubelet's ownership
change on remount is the likely cause. Code that requires a private directory on a volume with
`fsGroup` must restore the mode itself (through the directory file descriptor, without following
symlinks) rather than assume the mode it created survives a remount.

## Suspended, Idle and no Pod is not yet Stopped

2026-11-13 CI: the operator idle-resume Pod case in #16244 called `ResumeEnvironment` once the Sandbox
showed `Suspended`, `Idle` and no diagnostic Pod. The call wrote nothing (Kubernetes GETs, no PATCH),
and the readiness wait expired after 300 seconds on the original Pod UID. Pod deletion and the
controller's status write are separate operations: `converge` can delete the Pod while `updateStatus`
still sees the old workload and leaves the stop stage empty. The lifecycle policy resumes an idle
suspension only from `Stopped`, so the early call found a running desired state and had nothing to
wake. This cause is read from the source; no response or Sandbox snapshot was captured.

Wait for `stop_stage == "Stopped"` before resuming, then expect `REQUESTED` with the next wake
generation. #16244 added both checks to `agentplane/system_tests/scenarios/suspend_resume.py`
(`test_suspend_resume.py` covers the wait) without relaxing the lifecycle policy, authorization,
optimistic fencing or the ban on replaying uncertain writes. The policy now also refuses a resume
before `Stopped` with `FailedPrecondition` (`agentplane/environments/lifecycle/SPEC.md`).
