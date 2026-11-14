# Runtime state observation and Gateway ownership

## Selected contract; Gateway cutover pending

The selected Runtime/Gateway contract is recorded in the
[Runner Gateway README](../runner_gateway/README.md#replica-ownership-decision); this plan tracks its
rollout and rollback.

The leader-elected Sandbox Controller observes the current Runtime directly and keeps facts in
memory keyed by Sandbox UID, workload incarnation and boot ID. For a VM, the incarnation is the
virt-launcher Pod UID. On leadership acquisition or stream replacement, it relists current workloads
and waits for an authenticated snapshot matching that identity before using facts or starting an idle
timer. Callback generation and identity must still be current when facts or timers are updated.

The authenticated Controller `WatchState(observe_only=true)` is the source that binds the workload
incarnation in a Runtime process. The Runtime listener is available before this watch arrives, and
each Runtime process creates its own fresh boot ID. The launch file contains the Sandbox identity,
not the workload Pod UID, so Runtime has no shared local source for that UID across Pod, VM and
Firecracker modes. The Controller's exact SVID and current workload observation provide that
authority. Runtime reserves the UID before invoking ordinary lifecycle adoption, then permits
same-UID retries after stream loss or an uncertain adoption result; it never changes the binding
within that process. For a replacement process, the Controller supplies the replacement workload
UID, and Runtime restores retained state through its normal lifecycle barriers.

Any authenticated Runner Gateway replica may call a Runtime once it has the Controller-bound
Sandbox UID, incarnation UID and boot ID. Calls with an absent or zero connection epoch authorize
against that binding independently of Gateway state-watch lifetime. Nonzero call epochs retain the
legacy live-watch checks during compatibility. A Gateway `WatchState` without an epoch or with zero
may observe only the already-bound UID; legacy nonzero Gateway watches are also limited to that UID.
No Gateway watch may establish or advance the Runtime's incarnation. Gateway SVID authentication
establishes service identity; Gateway still enforces caller and per-method authorization. Dispatcher
delivery Leases remain a separate mechanism and are out of scope.

The Controller uses a direct passive Runtime state stream with its own narrow SVID; it does not
install Gateway call ownership. `Quiesce` continues through the Gateway. Runtime restarts, workload
replacement and same-name Sandbox replacement invalidate old calls and facts through the boot ID,
incarnation UID and Sandbox UID respectively. Another authenticated Gateway can call the current
Runtime without relying on a connection-epoch watch, but stale callers cannot.

The current workloads preserve the UID binding through their normal lifecycles: Firecracker RAM
restore uses the same launcher Pod; KubeVirt stop/start creates a new VMI, launcher Pod and Runtime
boot. KubeVirt live migration that replaces the launcher Pod while preserving the Runtime has not
been implemented or tested here. Supporting that case would require a separate trusted migration
authority to transfer the binding; a new Gateway watch is not such an authority.

## Loss, lifecycle and outcomes

Use the current 10-second snapshot heartbeat and 30-second observation timeout as the initial
freshness window. On stream loss, retain the last same-incarnation snapshot as stale for status,
retry after one second, and reset the idle timer. Stale facts do not prove connectivity, quiescence or
idleness; after the grace window report the Runtime disconnected. A fresh snapshot for the current
boot restores observation and begins a new idle interval.

Persisted `QuiesceAttempt` and its retry deadline survive leadership change and stream loss. Do not
clear it, stop the Pod or release its state volume from a stale `quiesced` fact. Keep terminal
bootstrap outcomes terminal for their incarnation; never rerun a succeeded, failed or interrupted
bootstrap because observation was lost. A replacement incarnation starts its own bootstrap state.

Gateway forwards each execution mutation at most once. If a call ends after Runtime may have acted,
the outcome is uncertain and failover, Runtime restart or reconnect never triggers automatic replay.
Dispatcher queue and command-delivery behavior remains owned by its separate delivery Lease.

## Migration and rollback

The rollout has three ordered Gateway stages plus a later compatibility cleanup. The Runtime
receiver landed in #16345; verify the target devel `runner-disk` pin contains that commit before the
first Gateway stage. A pin updates templates for future Runtime processes; it does not upgrade a
Pod, VMI or Firecracker guest already running. The Gateway receiver stage may merge and roll only
after the pinned image is active and older live staging Runtimes have been inventoried and cycled.
No staging inventory or cycling evidence is recorded in this plan yet.

### Staging Runtime inventory and cycle gate

Build one row per Agentplane Sandbox in `agentplane-staging`, including suspended and stopping
objects, then mark whether it currently has a Runtime process or a retained Firecracker checkpoint.
Start from `sandboxes.agentplane.allegedly.works` and record the Sandbox name and UID, template,
`status.operatingMode`, `status.stopStage`, `status.incarnation`, and
`status.runnerCredential`. Join child objects by owner UID and the `agentplane.allegedly.works/sandbox`
label; do not join on name alone. Read Pods, VirtualMachines, VMIs and their virt-launcher Pods so
that the actual incarnation UID and image references are tied back to each Sandbox UID. A suspended
Pod or KubeVirt VM has no live Runtime and will use the current template on its next start. A
suspended Firecracker Sandbox retains its Pod and RAM checkpoint, so include its Runtime as old until
proved otherwise.

The staging read should use the exact Agentplane resource and label, for example:

```sh
kubectl get sandboxes.agentplane.allegedly.works -n agentplane-staging -o json
kubectl get pods -n agentplane-staging -l agentplane.allegedly.works/sandbox -o json
kubectl get virtualmachines.kubevirt.io -n agentplane-staging -l agentplane.allegedly.works/sandbox -o json
kubectl get virtualmachineinstances.kubevirt.io -n agentplane-staging -l agentplane.allegedly.works/sandbox -o json
```

Extract only the fields below from those objects; Sandbox specs can contain bootstrap text and should
not be copied wholesale into the inventory. Resolve the Sandbox-to-Pod/VMI chain by owner UID and
check `status.runnerCredential.lastVmiUid` when matching the accepted VM incarnation.

Record the Runtime artifact source for each backend:

- Pod Sandbox: the Runtime binary is mounted from the runner disk image volume, normally
  `spec.volumes[name=environment].image.reference`. The Environment Runtime container's
  `status.containerStatuses[].imageID` identifies the tools container image, not the mounted
  Runtime disk.
- KubeVirt Sandbox: record the VMI's `spec.volumes[].containerDisk.image` for the runner disk, and
  correlate the VMI UID with `status.runnerCredential.lastVmiUid` and its virt-launcher Pod. The
  virt-launcher container's `imageID` is not the guest Runtime disk.
- Firecracker Sandbox: record the launcher Pod's runner image volume reference
  (`agentplane-firecracker-runner`), boot image volume, launcher container image and Pod UID. The
  Runtime executes inside the guest from the runner disk. The supervisor status exposes Pod UID and
  phase, while `EnvironmentRuntimeStatus` exposes a boot ID; neither reports the loaded Runtime
  build or resolved image-volume digest.

Compare these captured workload references with the target devel pin in
`cluster/k8s/agentplane-staging/image-pins/kustomization.yaml`. Kubernetes workload status does not
report the resolved OCI digest for image volumes, and Runtime status has no build-version field. If
the workload reference is a mutable tag, its text alone does not prove the bytes loaded by a live
Runtime; request the resolved digest from registry or node image metadata, or cycle it to a known
pinned image and verify the new incarnation and boot ID. A `GetStatus` boot ID proves process
identity, not source version.

For old Pod and KubeVirt Runtime processes, use the Sandbox Controller's normal stop/resume path so
it seals work, keeps the Sandbox UID and state PVC, and starts a new Pod or VMI from the current
SandboxTemplate. Verify a new workload UID, Runtime boot ID and connected/ready status before
counting that Sandbox cycled. A Firecracker suspend/resume restores the same Runtime process and
cannot adopt a newer runner disk. Replacing a Firecracker launcher while it retains a checkpoint is
not supported; keep the cutover blocked if an old Firecracker checkpoint is found. Do not claim a
Sandbox cycle from a template pin change or a still-existing Pod/VMI specification.

### A. Independent Runtime connections

Merge and pin the Gateway receiver that opens an independent zero-epoch `WatchState` and calls every
Runtime using Sandbox UID, workload UID and boot ID. It does not use Gateway Lease/epoch data to
authorize Runtime watches or calls. Retain the existing Lease/EndpointSlice publication only to
keep old per-Sandbox callers routed during this stage; its route epoch is not Runtime call ownership.
The receiver-image pin and completed live-Runtime cycle above are merge and rollout holds. Preserve
the old route records and permissions for rollback.

### B. Stable-Service callers

After every Gateway replica runs stage A, update caller defaults to the stable `runner-gateway`
Service. The Gateway replicas continue publishing the old per-Sandbox routes while B caller images
roll out, so callers still on the old address reach a compatible Gateway. Do not publish the stable
route to old Gateway replicas that still require exclusive Runtime watch ownership.

### C. Route-record removal

After every B caller has rolled out and no caller targets `<sandbox>-control`, remove the Gateway's
Lease and EndpointSlice route publication. All Gateway calls now use the stable Service, with
Runtime fencing by Sandbox UID, incarnation UID and boot ID. Dispatcher delivery Leases remain
unchanged. Keep the old route objects and permissions through the Gateway rollback window.

### Later compatibility cleanup

Only after C is pinned and the old Gateway rollback window has closed, remove unused connection-Lease
RBAC and the receiver's legacy nonzero epoch fields/tolerance in a separate image-pin change. Keep
leader-election Lease permission and Dispatcher delivery Lease behavior as required by their own
owners. No Runtime-to-Gateway dial is introduced.

Rollback from A or B requires restoring a Gateway image compatible with the deployed receiver and
its route records. Rollback from C first restores the route publication and per-Sandbox caller
configuration before deploying a Gateway that expects those routes. Until the later compatibility
cleanup, the Runtime continues accepting both zero-epoch independent calls and legacy nonzero calls
for the Controller-bound incarnation. A Runtime without the Controller binding cannot be made
compatible by falling back to Gateway identity; restart it with the receiver and let the Controller
bind the current UID.

## Acceptance

Acceptance must cover authenticated Controller-only initial binding, same-UID reconnect, refusal to
change UID after adoption begins or an uncertain callback, stale callbacks/snapshots, idle reset,
Sandbox/incarnation/boot fencing, epochless calls and watches against the bound UID, and legacy
nonzero Gateway behavior for that UID. It must also verify watch loss does not cancel epochless calls
and uncertain mutations are not replayed. The follow-on multi-replica system coverage must exercise
failover, Runtime restart, same-name replacement and independent Dispatcher delivery.
