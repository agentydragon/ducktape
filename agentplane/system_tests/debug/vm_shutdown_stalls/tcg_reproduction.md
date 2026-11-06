# Reduced software-emulated guest shutdown

This is a smaller diagnostic than the VM suite. It boots one guest directly in a
restricted throwaway Kubernetes pod, with no k3s, KubeVirt, application stack, sessions,
or harnesses inside the pod. It uses the actual CI guest kernel, initramfs, root and
runner disk, two QEMU TCG vCPUs, a disposable workspace block disk, and a virtiofs
`/state`. Configuration, trust and launch disks are **empty placeholders**, and the
Agentplane runtime is not exercised. It therefore cannot establish that a passing run
fixes or excludes the original CI stall. The QEMU devices, host kernel, scheduling and
shutdown controller also differ from CI.

On 2026-11-06 the initial run on `ovh-ns103656` reached `agentplane-acpi.service` and
`multi-user.target`. QEMU 10.1.0-10.el9 reported `kvm support: disabled` and two vCPU
threads. After `system_powerdown`, the guest recorded the ACPI request, unmounted
`/state/workspaces` and `/state`, emitted `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, and
logged `reboot: Power down` at guest uptime 65.596 seconds. QEMU's next status was
`paused (shutdown)`; the driver then quit it. Raw logs are in `tcg/initial-*.log.gz`.
The decompressed SHA-256 values, in serial/monitor/virtiofsd order, are
`fe6bcc9a18ecab4a23a33e51c7879a2275f87472152ee40498295d833f41bba2`,
`b28e6e54eec9870f74227dda4c07153c98ee8b6f74595b8867ae1b30f6d250e3`, and
`7f82b6f97136454c60c3e6defcf587981383636c20fb989949898dffd462a3ae`.
A separate rerun of the packaged setup on node `wyrm2` also completed shutdown.
These healthy runs are not a reproduction of the late shutdown stall.

KubeVirt's [`useEmulation`](https://kubevirt.io/api-reference/v1.6.0/definitions.html)
allows software fallback when hardware virtualization is unavailable; it is a
cluster-wide configuration, not a per-VM force-TCG flag. The shared cluster currently
has KVM available and the setting was not enabled. This experiment did not change its
KubeVirt installation. CI's own k3s fixture uses KubeVirt with software emulation;
the separate one-guest CI diagnostic exercises that integration.

## Run the reduced experiment

The input hashes and sizes below identify blobs from the CI execution. They may expire
from the BuildBuddy CAS; if so, obtain matching images from a current execution and
update `tcg/input.sha256` deliberately rather than silently substituting images.
Commands assume the repo devshell, BuildBuddy credentials, `kubectl` credentials and a
scratch directory outside the checkout. The pod uses the pinned KubeVirt launcher
image, denies pod ingress and egress, has no `/dev/kvm`, drops capabilities, and uses
bounded CPU, memory, ephemeral storage and lifetime. It creates a new namespace and
will delete only that namespace after verifying its UID. The namespace argument must not
already exist. On interruption or a cleanup failure, check that namespace explicitly.

```bash
scratch=/tmp/agents/YYYY_MM_DD/vm-shutdown-repro/tcg
mkdir -p "$scratch/inputs" "$scratch/unpacked"
bb download f19a2db8eb56a04b4022e836ba56d76be9d47844920a3d121fe13cfb74edb883/15380480 > "$scratch/boot.tar"
bb download 1b07d2974983aa8e519f72db321a93353eacd06f20184b8549788451a4bca637/553850880 > "$scratch/root.tar"
bb download 1582e116089005a2ff59999307362a1a5b70a85d2f4b36f076867fa100276674/72898560 > "$scratch/runner.tar"
tar -xf "$scratch/boot.tar" -C "$scratch/unpacked"
tar -xf "$scratch/root.tar" -C "$scratch/unpacked"
tar -xf "$scratch/runner.tar" -C "$scratch/unpacked"
cp "$scratch/unpacked/boot/vmlinuz" "$scratch/unpacked/boot/initramfs.cpio" "$scratch/inputs/"
cp "$scratch/unpacked/disk/root.img" "$scratch/unpacked/disk/test-runner.img" "$scratch/inputs/"
agentplane/system_tests/debug/vm_shutdown_stalls/tcg/run.sh \
  NEW_DISPOSABLE_NAMESPACE "$scratch/inputs" "$scratch/output" 5
```

The driver bounds each image transfer at 180 seconds; a failed transfer aborts and
cleans up the namespace. It also stops the batch if the guest exec itself times out,
so an unknown prior QEMU process cannot overlap the next guest. The guest script
verifies the four extracted input hashes before booting. It sends the
power button after 60 seconds and quits QEMU 30 seconds later. Its exit check requires an ordered request, receipt and kernel powerdown in the same
serial stream, plus monitor evidence of disabled KVM and a paused shutdown. Inspect
the retained raw output for errors as well. The fixed 60-second boot delay
can produce a false failure on a loaded node if the ACPI handler is not ready yet.
The optional final argument runs one to ten guests sequentially in the same pod; each
starts with a fresh workspace and state volume. The output directory contains pod
preflight and event logs plus serial, monitor and virtiofsd logs in each run directory.

## Other contemporaneous CI controls

[Normal logging](https://app.buildbuddy.io/invocation/fef23c09-4a34-4204-9742-17a3bd315fe5)
produced seven passes and one pytest timeout after lengthy setup; all guests in the
failed execution emitted shutdown proof and powered down.
[Boot-time systemd debug logging](https://app.buildbuddy.io/invocation/af4d68d2-e342-478e-97af-4a500f9c6625)
produced six passes. Neither batch reproduced the late post-ACPI stall. Both also had
separate outer BuildBuddy Firecracker kernel panic attempts that passed on infrastructure
retry; those are distinct failures and no causal link to the guest stalls is established.

## Repeated controls and CPU contention

On 2026-11-06, five fresh guests ran sequentially on node `wyrm2` using the packaged
driver, followed by five more on the same node with one `yes` process sharing the
pod's one-CPU cgroup limit during the 30 seconds after the power-button request. Both
arms used the same pinned launcher image and guest input hashes. Each guest started with
fresh state and workspace storage. Every guest in both arms recorded the ordered shutdown
receipt and powerdown, and every QEMU monitor recorded `kvm support: disabled` followed
by `VM status: paused (shutdown)`. Neither arm reproduced the late stall.

| Arm            | Runs | First watchdog uptime to kernel powerdown |
| -------------- | ---: | ----------------------------------------: |
| Control        |    5 |                         2.31–2.64 seconds |
| CPU contention |    5 |                         4.10–4.67 seconds |

The interval subtracts the uptime in the first shutdown-watchdog sample from the kernel
powerdown uptime. It is an approximation, not a precise duration from the ACPI event:
the sample itself can be delayed or interleaved. The small sample shows that the added
load slowed these shutdowns, not that it causes the original failure.

The unmodified per-run serial, monitor, virtiofsd, and driver output are retained in
[control.tar.gz](tcg/results/control.tar.gz) and
[cpu-pressure.tar.gz](tcg/results/cpu-pressure.tar.gz). Both owned namespaces were
verified absent after cleanup. The pressure arm's exact guest-script change is in
[cpu-pressure.patch](tcg/results/cpu-pressure.patch); it starts the CPU consumer just
before the power-button command and kills it before QEMU exits. To repeat it, copy the
scripts to scratch, apply the patch there, and choose a fresh namespace:

```bash
mkdir -p "$scratch/pressure"
cp agentplane/system_tests/debug/vm_shutdown_stalls/tcg/{run.sh,guest.sh,check.sh,pod.yaml,input.sha256} "$scratch/pressure/"
patch -d "$scratch/pressure" -p1 < agentplane/system_tests/debug/vm_shutdown_stalls/tcg/results/cpu-pressure.patch
"$scratch/pressure/run.sh" NEW_PRESSURE_NAMESPACE "$scratch/inputs" "$scratch/pressure-output" 5
```

For a matched same-node comparison, pin both pod manifests to the same suitable node;
the committed pod manifest intentionally leaves scheduling unconstrained.

## Repeated ACPI requests in the original failure

In [the original CI failure](https://app.buildbuddy.io/invocation/15e9310a-7352-56a3-87c6-322dfb3065c0),
all three guests logged four closely spaced `AGENTPLANE_GUEST_SHUTDOWN_REQUESTED`
lines. The [VM 1 console](tcg/results/original-vm-1-console.log.gz) has no shutdown
receipt or kernel powerdown; [VM 2](tcg/results/original-vm-2-console.log.gz) and
[the Codex OOM guest](tcg/results/original-vm-3-console.log.gz) both completed.
The decompressed consoles have SHA-256 digests `6175cadfbea61e5e77c782296bf9f21839333c20af6919479ed4c5f5929a6522`,
`2e0879963edacffd46f9c7a2cffda1d94058b59aa40241bb37b3eb67b44566d9`, and
`947bf2c70033f04f025553038a55d99be6eb7058bbdb5baef35ebb71d6ad691a`, respectively,
matching the BuildBuddy CAS artifacts. Multiple requests are therefore a feature to
preserve in the reduced experiment, but occurred on both failed and healthy guests and
are not sufficient evidence of the cause.
