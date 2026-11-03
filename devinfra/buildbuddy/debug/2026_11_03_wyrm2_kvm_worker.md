# wyrm2 KVM worker validation

The manual `wyrm2-kvm-vm` pool runs Docker actions inside a static Ubuntu KubeVirt VM.
The VM is the boundary from the main cluster; actions in this trusted-code pool can
control the persistent guest. The existing hosted lane runs actions inside BuildBuddy
Firecracker guests. Both use the same K3s fixture and VM scenarios.

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
which asks the nested runtime for all capabilities. The proposed worker configuration
therefore supplies `docker_cap_add: ALL` inside the dedicated guest. Full VM-scenario
acceptance with this capability set is still pending.

The recreated guest reached BuildBuddy over HTTPS, timed out connecting to `example.com`,
and received a connection reset for the wrong TLS hostname directed to BuildBuddy's IP.
Cilium endpoint 1167 was ready with ingress and egress policy enabled. These observations
do not include a separate negative ingress test.
