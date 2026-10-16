# Runner image layering spike

This package is a bounded experiment for `RUNNER_IMAGE_LAYERING_DESIGN`. It asks what can stay in
the booted runner environment, what must live in a separately updated tools filesystem, and what a
real Codex process needs when its child filesystem starts almost empty.

The prototype builds independent boot, harness, and tools inputs:

- `base_initramfs`: the guest's small PID 1 initializer;
- `tools_rootfs`: a separately built Debian Trixie OCI filesystem containing Bash and its runtime
  dependencies;
- the pinned static Codex executable and bwrap are staged as a separate file set;
- `local_vm_initramfs`: one CPIO archive containing PID 1 and the harness payload for the local
  Firecracker probe. The base initializer and harness files remain independent build inputs.

The initializer stays outside the chroot. The runner's test-only `child_exec` fence creates a user,
mount, and PID namespace, makes its root read-only, then bind-mounts the workspace and
session-specific Codex home and mounts `/proc`, `/dev/null`, and a small temporary filesystem. It
execs the real pinned Codex binary from `/opt/bin/codex`, beside the separately staged
`/opt/codex-resources/bwrap` payload expected by the Codex distribution. Runner state and the runner
process stay outside that child root.

The focused `//agentplane/runner:test_restart` scenario runs the pinned Codex binary from the sparse
child root, completes and resumes a conversation after killing the native harness process, then
executes `printf SPARSE_ROOT_TOOL_OK` through Codex's shell tool using only the separate Bash tools
rootfs. The command output is asserted by the test. The child cannot read a runner-only sentinel,
and killing the child does not stop the runner.

## Current limits

The local VMM target uses the repository-pinned Firecracker 1.12.1 binary, a digest-pinned
Firecracker 1.12 CI `vmlinux-6.1.128` ELF input, and the host's KVM. The kernel comes from the
official [Firecracker CI artifact bucket](https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128),
so the probe does not depend on the host kernel's image format. The kernel is an experiment input,
not a production kernel choice.

The local VM probe boots with 1 GiB guest RAM, runs `codex --version` inside the sparse chroot, and
confirms the guest's parent init remains alive after Codex exits. The additional memory is required
because the pinned static Codex executable alone is about 285 MB and the initramfs also holds the
kernel's reserved memory. The CPIO generator now emits parent directories explicitly; the kernel
initramfs extractor did not create nested directories that were absent from the archive.

Measured outputs are 1,913,348 bytes for `base_initramfs`, 22,277,632 bytes for the Bash OCI layer
tar, and 287,783,968 bytes for `local_vm_initramfs`. The tools-root target has no dependency path
from `local_vm_initramfs` in Bazel. A warm BuildBuddy build of the base and tools targets took 5.577 s
(3.90 s critical path). Three fresh guest launches under `perf stat --repeat 3 -- bb run ...`
averaged 2.231 ± 0.066 s wall time with warm build artifacts; this includes Bazel's local-run
overhead, so it is not a guest-only boot number. The first local materialization of the VM target
took 24.776 s Bazel elapsed while downloading the 287.8 MB initramfs. Attempts to force a cache miss
still showed action-cache hits, so a true cold build time is not claimed.

Claude remains a separate dynamic-linking case; its glibc loader closure has not been tested in this
child. Conventional Debian and Nix environment compatibility, a full coding task, cold/warm layer
preparation timings, and production packaging remain open. This prototype makes no production VMM,
image-layout, or isolation certification decision.

No production runner image, Kubernetes resource, Sandbox, VM, or cluster state is changed here. The
spike does not select Firecracker over KubeVirt, define an arbitrary-rootfs support contract, or
claim complete filesystem isolation. The child namespace is a feasibility probe; it does not replace
the production process, cgroup, network, and syscall policy.

## Commands

Build the artifacts with the normal BuildBuddy-backed Bazel configuration:

```bash
bbr build //agentplane/x/runner_layering_spike:base_initramfs
bbr build //agentplane/x/runner_layering_spike:tools_rootfs
bbr build //agentplane/x/runner_layering_spike:local_vm_probe
```

The full-turn, shell-tool, and resume evidence runs with:

```bash
bbr test //agentplane/runner:test_restart --test_arg=-k --test_arg=sparse_child_root
```

The local-only VMM probe uses the pinned kernel and host `/dev/kvm`:

```bash
bb run //agentplane/x/runner_layering_spike:local_vm_probe
```

The VM launch is intentionally local because it needs the host's `/dev/kvm`. Remote Bazel actions
remain enabled for the build and test work.
