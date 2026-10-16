# Runner image layering spike

This package is a bounded experiment for `RUNNER_IMAGE_LAYERING_DESIGN`. It asks what can stay in
the booted runner environment, what must live in a separately updated tools filesystem, and what a
real pinned Codex process needs when its child filesystem starts almost empty.

The prototype builds independent boot, harness, and workload-tools inputs:

- `base_initramfs`: the guest's small PID 1 initializer;
- `tools_rootfs`: the separate `workload_tools_image` OCI filesystem. Its Debian Trixie Bash/runtime
  closure and `workload-tool` executable are image layers, separate from the runner and harness;
- the pinned Codex, bwrap, and Claude executables are staged into a separate harness filesystem;
- the local Firecracker guest receives the small PID 1 CPIO plus two separate, read-only SquashFS
  drives for tools and harness files.

The guest initializer stays outside the child chroot. It mounts the tools drive at `/tools`, the
harness drive at `/tools/opt`, then supplies writable tmpfs homes, workspace, and `/tmp`, plus
read-only child procfs. The child runs as uid/gid 107 from `/tools`; the harness files remain
independently packaged under `/opt`. The runner integration test separately creates a sparse child
root under user and mount namespaces, keeps runner state outside it, and checks that a child cannot
read a runner-only sentinel.

The focused `//agentplane/runner:test_restart` scenario runs the pinned Codex binary from the sparse
child root. A mocked model asks Codex to execute `/usr/local/bin/workload-tool` from the independent
workload image; the test checks its output and the proof file it writes through the workspace mount.
It then kills the native harness, resumes the same session, reads that file from the persistent
workspace, and asks the resumed Codex process to execute the workload tool again. Runner-private
state remains outside the child root. The local VM probe separately runs
Bash and Codex from the two mounted filesystems; guest PID 1 remains alive after both exit. Its Claude
probe currently aborts in the guest, and the parent records that failure so it remains inspectable.

Production image-selection policy remains open. The test selects the workload image as a separate
Bazel input while the runner and pinned harness targets remain unchanged.

## Current limits

The local VMM target uses the repository-pinned Firecracker 1.12.1 binary, a digest-pinned
Firecracker 1.12 CI `vmlinux-6.1.128` ELF input, and the host's KVM. The kernel comes from the
official [Firecracker CI artifact bucket](https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128),
so the probe does not depend on the host kernel's image format. The kernel is an experiment input,
not a production kernel choice.

The local VM probe boots with 1 GiB guest RAM, mounts the tools and harness filesystems read-only,
runs Bash and `codex --version`, and confirms guest PID 1 remains alive after those child processes
exit. The additional memory is required because the pinned static Codex executable alone is about
285 MB and the guest also holds the kernel's reserved memory. The CPIO generator emits parent
directories explicitly because the kernel initramfs extractor does not create nested directories
that are absent from the archive.

The original probe measured 1,913,348 bytes for `base_initramfs`, 7,168,000 bytes for the tools
SquashFS, and 168,894,464 bytes for the harness SquashFS. The Bash OCI layer tar was 22,277,632
bytes. These are compressed artifact sizes, not unpacked guest memory or cold build-time
measurements; they predate the small workload-tool layer added to the tools image. The Codex binary
makes the harness layer the largest artifact. Earlier combined-initramfs measurements are
superseded by these separate-disk outputs.

Claude 2.1.252 prints its Bun runtime information, then aborts with SIGABRT when invoked as
`--version` inside the guest chroot, even though the Debian tools root contains its glibc loader and
libraries. The exact same pinned executable exits successfully with `--version` from the full host
root and from the guest chroot as root without the child uid or mount namespace. It aborts in the
uid-107 child with a new mount namespace. This narrows the problem to that child execution context;
the individual UID and namespace effects have not yet been separated. An upstream report describes
a similar startup abort after a Bun update, but does not establish the cause here:
[Anthropic issue 92749](https://github.com/anthropics/claude-code/issues/92749).

The Pod comparison did not launch the workload in the disposable `v1.33.4+k3s1` K3s stack, which
used containerd `v2.0.5-k3s2`. With `ImageVolume=true` on the API server and kubelet, both OCI images
were present and the API accepted the Pod, but containerd repeatedly failed container spec creation
with `failed to generate spec: failed to mkdir "": mkdir : no such file or directory`; the Pod
remained Pending with `CreateContainerError`. This records a failure in this test stack, not a
general claim about Kubernetes image volumes. No runtime upgrade or alternate Pod mechanism is part
of this spike. Conventional Nix environment compatibility, a full coding task, cold/warm layer
preparation timing, and production packaging remain open. This prototype makes no production VMM,
image-layout, or isolation certification decision.

No production runner image, Kubernetes resource, Sandbox, VM, or cluster state is changed here. The
spike does not select Firecracker over KubeVirt, define an arbitrary-rootfs support contract, or
claim complete filesystem isolation. The child namespace is a feasibility probe; it does not
replace the production process, cgroup, network, and syscall policy.

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
