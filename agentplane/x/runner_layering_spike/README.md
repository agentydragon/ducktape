# Runner image layering spike

This package is a bounded experiment for `RUNNER_IMAGE_LAYERING_DESIGN`. It asks what can stay in
the booted runner environment, what must live in a separately updated tools filesystem, and what a
real Codex process needs when its child filesystem starts almost empty.

The prototype builds three artifacts:

- `base_initramfs`: the guest's small PID 1 initializer;
- `tools_rootfs`: a separately built Debian Trixie OCI filesystem containing Bash and its runtime
  dependencies;
- the pinned static Codex executable is staged separately into the test child root;
- `local_vm_initramfs`: the concatenated initramfs layers used by the local Firecracker probe.

The initializer stays outside the chroot. The runner's test-only `child_exec` fence creates a user,
mount, and PID namespace, makes its root read-only, then bind-mounts the workspace and
session-specific Codex home and mounts a small temporary filesystem. It execs the real pinned Codex binary from
`/opt/codex`. Its first startup failure identified `/proc/self/exe` as a Codex requirement, so the
child also receives a minimal `proc` mount. Runner state and the runner process are outside that
child root. The focused
`//agentplane/runner:test_restart` scenario exercises a completed model turn, a child exit, runner
survival, and Codex conversation resume through this fence.

The first baseline used the pinned static Codex executable without a shell or distribution base.
After that baseline completed a turn and resumed, the tool-call probe added a separate Bash-only
filesystem. Its current process-creation failure remains unresolved; no other component has been
added. Claude is a separate dynamic-linking case; its observed glibc loader closure does not belong
in Codex's minimal root by default. Existing complete Debian and Nix runner environments are
compatibility cases after this minimal experiment, not ingredients in its first root.

## Current limits

The local VMM target uses the repository-pinned Firecracker 1.12.1 binary and the host's KVM. The
host's current kernel is a compressed `bzImage`; Firecracker requires an ELF `vmlinux`. No compatible
kernel output is available in this checkout, and building one is outside this spike. Therefore the
initramfs is buildable, but this host cannot yet provide guest-boot evidence. A passing chroot test is
not evidence that the Firecracker guest booted.

The sparse child completes a Codex text turn, survives child termination, and resumes the persisted
Codex conversation while keeping a runner-only sentinel outside the child root. The next probe asks
Codex to run one shell command with the Bash-only tools filesystem extracted into that root. That
tool call currently fails during process creation with `No such file or directory (os error 2)`;
the test teardown also reports the resulting unsettled model exchange. The image builds, but this
does not yet establish that the shell or its runtime closure is usable from the child. The missing
path or dependency still needs to be identified from the concrete process setup.

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

The full-turn and resume evidence runs with:

```bash
bbr test //agentplane/runner:test_restart
```

If an ELF `vmlinux` is available, the local-only VMM probe runs with:

```bash
bazelisk run //agentplane/x/runner_layering_spike:local_vm_probe -- --kernel /path/to/vmlinux
```

The VM launch is intentionally local because it needs the host's `/dev/kvm`. Remote Bazel actions
remain enabled for the build and test work.
