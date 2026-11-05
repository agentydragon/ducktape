# wyrm2 KVM worker validation

The manual `kvm-vm` pool runs Docker actions inside a static Ubuntu KubeVirt VM.
The VM is the boundary from the main cluster; actions in this trusted-code pool can
control the persistent guest. The existing hosted lane runs actions inside BuildBuddy
Firecracker guests. Both use the same K3s fixture and VM scenarios.

**Adoption is deferred.** The November 4 run below proves that the existing
worker can execute all three scenarios with KVM, but its target remains red
and execution still takes over ten minutes. Keep this lane manual and
experimental; these results do not justify moving ordinary CI onto our VM.

## BuildBuddy guest comparison

Inspected the executor's pinned BuildBuddy v2.321.0 source, commit
`1d2cfd8b2b8e3c37342c68b972f1218247ff00d4`:

- [`goinit`](https://github.com/buildbuddy-io/buildbuddy/blob/v2.321.0/enterprise/server/cmd/goinit/main.go)
  mounts procfs, sysfs and cgroup2 directly inside its VM, without Docker's read-only
  `/proc/sys` and cgroup mounts. It uses ext4 block devices for its root/scratch/workspace
  filesystems and runs `vmexec` without starting the image's systemd.
- [`vmexec`](https://github.com/buildbuddy-io/buildbuddy/blob/v2.321.0/enterprise/server/vmexec/vmexec.go)
  runs the requested command directly, switching credentials only when a user is specified.
- The Docker executor adds configured capabilities, devices and volumes to Docker's
  normal container setup. It has no sysctl configuration flag in this pinned version.

K3s working in the hosted guest therefore does not establish that it can boot inside a
Docker action. The static worker supplies guest-wide kernel settings at boot, while the
KVM fixture makes only its private network sysctls and cgroup subtree writable. The
cgroup setup moves initial processes into a child before enabling controllers, following
the [kind entrypoint](https://github.com/kubernetes-sigs/kind/blob/main/images/base/files/usr/local/bin/entrypoint).

## Observed rollout boundaries

1. Docker 29's containerd image store filled the 2.4 GiB root filesystem while pulling
   the executor image. Setting Docker's `data-root` alone did not move it. Moving
   containerd's root to the ext4 work disk allowed the executor to register with its
   existing BuildBuddy key. This configuration landed in #14086.
2. The [KVM smoke run](https://app.buildbuddy.io/invocation/f0c41bcd-8829-404c-8d72-4b0fce61f4d9)
   passed uncached on worker `c13ae91a-e854-4da1-b28b-8ad3acad8db6`: API version 12,
   VM creation and vCPU creation. This does not prove a VM scenario passes.
3. K3s's overlayfs snapshotter rejected Docker's action overlay filesystem. An ext4
   bind for `/var/lib/k3s-env` let it proceed to kubelet initialization.
4. Kubelet needed `vm.overcommit_memory=1`, `kernel.panic=10` and
   `kernel.panic_on_oops=1`. Setting these in the worker guest let it start without
   exposing the guest-wide `/proc/sys` writable in the action.
5. [The next run](https://app.buildbuddy.io/invocation/717b9ad7-87de-471d-a320-067973cbabd7)
   reached kube-proxy, which needed to set `route_localnet`. An RBE probe verified a
   writable bind of only `/proc/sys/net` inside the action's network namespace.
6. [The following run](https://app.buildbuddy.io/invocation/60259a64-842f-4213-83fd-d636e11719d9)
   started K3s and imported fixture images, but bundled pods tried to pull pause from
   Docker Hub, which the outer fence denied. It was canceled after diagnosing this.
   #14094 supplies K3s registry mirrors through Zot and disables public fallback.

A cold Zot pull also failed once with an octet-stream media type while resolving the
RBE image. Later warm pulls and the smoke test succeeded. The original cold response
headers were not captured, so the precise cause remains unproven; no image repin or
Zot change was made.

## Fresh provision verification

Flux applied #14094's merge `e454d0c8764deb8af0ff67ecce81dff8024eccaa`. The disposable
worker was recreated without live repairs. Cloud-init completed, applied the expected
kernel settings and containerd storage root, rebooted with `apparmor=0 loop.max_loop=64`,
and started the executor. It registered with the existing key and became ready at
09:20:11 UTC, worker `4d8716b2-7b9a-41dc-a87b-e0b6399cf115`.

The [fresh-worker run](https://app.buildbuddy.io/invocation/86bd3bbd-a374-4c78-863e-b7b5cd513e6d)
passed the uncached KVM smoke action. The full suite pulled its bundled pod image through
Zot but nested runc failed with `failed to update /proc/self/oom_score_adj: Permission denied`.
The action had Docker's default capabilities plus `SYS_ADMIN,NET_ADMIN,MKNOD`; lowering
OOM scores requires `SYS_RESOURCE`.

KubeVirt v1.8.2 also runs its
[node agents privileged](https://github.com/kubevirt/kubevirt/blob/v1.8.2/pkg/virt-operator/resource/generate/components/daemonsets.go),
which asks the nested runtime for all capabilities. The worker configuration therefore
supplies `docker_cap_add: ALL` inside the dedicated guest. Full VM-scenario acceptance
remains pending; the latest revalidation below cleared the Docker seccomp blockers
and exposed an image-cleanup failure.

The recreated guest reached BuildBuddy over HTTPS, timed out connecting to `example.com`,
and received a connection reset for the wrong TLS hostname directed to BuildBuddy's IP.
Cilium endpoint 1167 was ready with ingress and egress policy enabled. These observations
do not include a separate negative ingress test.

## Default Docker seccomp blocks nested runc

After the worker update was reported ready, the
[unmodified devel run](https://app.buildbuddy.io/invocation/fa0471c2-a9d1-4d0a-a3c7-a1c2d73db333)
at `22771892d0251c0432fb94889857a506c8d52751` failed before scenarios. Nested runc
reported `unable to create session key: operation not permitted` for supporting
Pods. The existing 180-second cert-manager rollout deadline then expired.

A [direct syscall probe](https://app.buildbuddy.io/invocation/ef144a2d-ffc7-47a8-86c5-a98be3b2a7e8)
had reproduced `KEYCTL_JOIN_SESSION_KEYRING` returning `EPERM` while KVM API version
12, VM creation and vCPU creation succeeded. Read-only inspection of the updated
worker showed Docker's built-in seccomp profile and no executor `SecurityOpt`
override. [Docker documents](https://docs.docker.com/engine/security/seccomp/) that
its default profile denies both `keyctl` and `pivot_root`, including when the
action has the capabilities needed for mounts.

A [fixture-only experiment](https://app.buildbuddy.io/invocation/a423660b-4e4c-44ba-9a22-8676498a194f)
at runtime commit `5d4422d221` enabled containerd's standard `NoNewKeyring` option.
The retained containerd startup log confirmed `NoNewKeyring=true`: session-key
creation errors stopped, exposing 74 `pivot_root .: operation not permitted`
errors. Supporting Pods still could not start, so cert-manager's rollout expired
and no guest scenario ran. The experimental fixture change was dropped; the
worker's runtime policy needs to support nested container startup.

Both full runs used worker `57dfd687-22ab-49e8-9bad-4b0fb1bf2fb5`. The unmodified
run spent 324.03 seconds waiting for a worker and 376.30 seconds executing. The
experiment waited 1,056.76 seconds behind another session's action, then executed
for 260.04 seconds. These times come from remote execution metadata; Bazel's test
summary reports the experiment's start before worker assignment. Neither failed
setup measures guest boot latency or establishes a CI speedup.

This validation session made no worker or outer-cluster configuration changes.
Before accepting the lane, rerun the full suite after the worker policy update
and verify all three scenarios, actual KVM acceleration, boot/shutdown evidence,
traces and cleanup.

## Revalidation after the worker policy update

[The keyring policy change](https://github.com/agentydragon/ducktape/pull/14207)
merged, and another session recreated the worker. At 05:37 UTC on November 4,
its live Docker profile also allowed `pivot_root` with `CAP_SYS_ADMIN`; inspection
of a running action's OCI configuration confirmed that rule was applied. The
profile in devel `040c86945b996d2187a96744b16bc9fa4b5bf8e4` still lacked that
additional rule, so this validates the live worker rather than reprovisioning
from that source revision. This session made no worker configuration changes.

The [fresh smoke](https://app.buildbuddy.io/invocation/eeee4cfa-23b9-4ecb-9d5f-d7d82556e4da)
passed in 7.73 seconds of execution: KVM API/VM/vCPU creation, runc's three keyring
operations, and a still-denied `KEYCTL_READ` operation.

The [full VM suite](https://app.buildbuddy.io/invocation/286e880e-d146-41ee-b804-c8019df85345)
used the same unmodified source and worker `6e2353c4-dc8c-4624-8116-233ea1e3eff0`.
Its retained runtime log contains no session-key or `pivot_root` failures.
Setup completed in 313.12 seconds and all three scenarios created their VM
resources, but their launcher Pods hit `ImagePullBackOff`: the fixture's cleanup
had deleted the newly imported pinned guest-root image. The
[image-cleanup investigation](../../../agentplane/system_tests/debug/cri_image_cleanup.md)
records the deletion sequence. No guest boot or complete scenario pass was established.
Execution took 640.49 seconds after 563.53 seconds of scheduler wait; these
failed-run timings do not establish a CI speedup.

## VM scenarios pass after image protection

The [fixture image protection](https://github.com/agentydragon/ducktape/pull/14226)
merged. A [fresh full run](https://app.buildbuddy.io/invocation/9b8d28ca-acda-448a-9c4f-5c96c3ca287e)
at unmodified devel `2eda822716e1f80de0869486ba57338111e248c5` used the same worker.
All three scenarios passed: Claude supervision, a Codex turn, and Codex OOM
survival with recovery in the same session. The process trace contract passed,
and console logs plus systemd journal artifacts were retained for all three guests.
The final CRI inventory retained both guest images with `pinned: true`.

Read-only inspection during this action found three `qemu-kvm` processes, each
with `-accel kvm`, an open `/dev/kvm`, a KVM VM descriptor and two KVM vCPU
descriptors. This establishes actual accelerated guest execution beyond the
earlier syscall smoke. The live `pivot_root` policy rule still was absent from
this source revision; fresh provisioning from devel alone remains unverified.

The Bazel target remained red: pytest reported **1 passed, 1 teardown error**.
Its 600-second deadline covered session fixture setup, the scenario body and
teardown. Setup took 273.93 seconds; the scenario body, including concurrent
per-sandbox cleanup, took 312.21 seconds. At 06:47:44 UTC the deadline interrupted
`crictl rmp --all --force` inside `K3s.cleanup_stopped`. Containerd's cleanup log
was still recording successful removals just before interruption, so this run
does not establish a cleanup wedge or completed final cleanup. The timeout and
all coverage checks were unchanged.

Remote execution metadata separates 438.06 seconds waiting for the single
worker from 611.06 seconds executing. The three sandbox readiness waits
overlapped and took 147.12–154.68 seconds each; their cleanup took 24.01–30.05
seconds each. These are one run's phase measurements, not a matched CI speedup.

### What the five-minute scenario phase contains

The 312.21-second case includes guest provisioning/readiness, scenario actions
and per-sandbox cleanup. The three concurrent branches took 284.08 seconds
(Claude), 287.01 seconds (Codex) and 311.31 seconds (Codex OOM recovery).
The OOM branch determined the case's elapsed time. Its sequential driver spans
were:

| Stage                           | Seconds |
| ------------------------------- | ------: |
| Wait for Sandbox readiness      |  147.12 |
| Open Codex session              |    7.56 |
| Establish baseline turn         |   75.57 |
| Trigger OOM and verify survival |   27.64 |
| Verify same-session recovery    |   29.19 |
| Sandbox cleanup                 |   24.01 |

These spans explain almost all of that branch's elapsed time. The
`test-spans.jsonl`, `process-spans.jsonl`, `vm-codex-oom.json`, launcher/console
logs and systemd journals remain linked to the invocation for deeper analysis.
The readiness wait includes provisioning and guest startup; it does not isolate
CPU emulation cost. KVM availability alone did not remove these long phases.

Before reconsidering adoption, establish successful bounded final cleanup,
reproduce the worker from committed configuration, and demonstrate a substantial
latency benefit with repeated matched hosted-versus-own-VM runs. Match source,
coverage, resources, concurrency and cache warmth; account for worker queueing
separately. No matched comparison or CI speedup is established by this run.

### Nearby hosted runs were faster without KVM

Two nearby physical executions of the same three concurrent VM scenarios passed
on hosted Firecracker workers. Both retained-infrastructure receipts report a
**cold** start, so these are not warm-attach timings. The hosted launcher log
reports `/dev/kvm` absent and software emulation in use.

| Measurement                                             | [Hosted, 06:15 UTC](https://app.buildbuddy.io/invocation/3eb9c543-00f6-54da-95f9-654f9ef5f843) | [Hosted, 06:32 UTC](https://app.buildbuddy.io/invocation/8e258ea7-9a5a-5bc9-960a-06f31f959bea) | [wyrm2 KVM](https://app.buildbuddy.io/invocation/9b8d28ca-acda-448a-9c4f-5c96c3ca287e) |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------: | ---------------------------------------------------------------------------------------------: | -------------------------------------------------------------------------------------: |
| Worker queue                                            |                                                                                         0.06 s |                                                                                         0.09 s |                                                                               438.06 s |
| Shared stack setup                                      |                                                                                       111.51 s |                                                                                       104.24 s |                                                                               273.93 s |
| Concurrent scenario body, including per-sandbox cleanup |                                                                                       155.21 s |                                                                                       148.01 s |                                                                               312.21 s |
| Final shared stack cleanup                              |                                                                              26.87 s, complete |                                                                              29.44 s, complete |                                                    Interrupted by the overall deadline |
| Remote action execution                                 |                                                                            **330.12 s (5m30)** |                                                                            **331.75 s (5m32)** |                                                  **611.06 s (10m11), failed teardown** |

The hosted executions started on November 4 at 06:15:22.850533 and
06:32:27.035790 UTC. Their action platforms requested eight estimated compute
units, Firecracker isolation and runner recycling. The first execution was PR
#14226's CI; a subsequent mainline result reused its exact artifacts and is not
a third independent execution. The second ran at `b227dabb9a`. The VM test,
scenario, stack and macro sources did not change between the image-protection
merge and the wyrm2 run at `2eda822716`.

In these observations, the wyrm2 action took about **85% longer**, and its
scenario phase took **2.0–2.1 times as long**. Its final cleanup did not complete,
so 611 seconds is not a successful full-suite timing. Queueing adds another
7m18 on the single-slot worker and is excluded from that execution comparison.

This is an observational comparison of two arrangements, not a controlled
KVM-versus-software-emulation benchmark. Host CPU scheduling, storage, runtime
configuration, resources and infrastructure lifetime differ. It does establish
that access to KVM did not make this worker arrangement faster in these runs.
Keep adoption deferred; making the target green alone would not demonstrate a
latency benefit.

Evidence comes from each invocation's TestRunner execution timestamps,
`test-spans.jsonl`, `persistence-suite.json`, test outcome and launcher log.
Setup, case and cleanup spans do not sum to the whole action: framework startup,
other fixture work and artifact finalization also contribute.
