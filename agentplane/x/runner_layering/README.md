# Runner image layering experiment

`//agentplane/x/runner_layering:test_sparse_root` runs pinned Codex in a sparse child root populated
from an independent Debian Bash and workload-tool OCI image. The test runs a real tool call, checks
its workspace write, kills Codex, and resumes the same session. The resumed process sees the prior
conversation and workspace file and runs the tool again. The Runtime survives the child exit, and
its private sentinel stays outside the child root. Codex needs child procfs, `/dev/null`, and a
logical working directory matching the child-visible workspace mount. The Sandbox remains the
production isolation boundary. [Workload-tool test and original RBE result](https://github.com/agentydragon/ducktape/pull/10789)

```bash
bbr test //agentplane/x/runner_layering:test_sparse_root
```

To build and retrieve the generated OCI layout locator Python module:

```bash
bbr build //agentplane/x/runner_layering:tools_rootfs --remote_build_event_upload=all
```

## Retired Firecracker boot experiment

[#10741](https://github.com/agentydragon/ducktape/pull/10741) booted Firecracker 1.12.1 with the
pinned Linux 6.1.128 ELF kernel and 1 GiB guest RAM. A small initramfs ran PID 1 outside the chroot.
It mounted read-only SquashFS tools at `/tools` and the harness drive at `/tools/opt`, then provided
tmpfs homes, workspace and `/tmp` plus read-only child procfs. Bash and pinned Codex
`--version` succeeded inside the child, and PID 1 stayed alive. The initramfs, tools disk and
harness disk measured 1,913,348, 7,168,000 and 168,894,464 bytes respectively. The tools disk
measurement predates the workload-tool layer.

Claude 2.1.252's `--version` aborted with SIGABRT in the uid-107 child with a new mount namespace.
The exact binary succeeded on the full host and in the guest chroot as root without that namespace.
The UID and namespace effects remain unresolved. The [disposable K3s Pod trial](https://github.com/agentydragon/ducktape/pull/10744)
with ImageVolume enabled accepted the Pod on `v1.33.4+k3s1`; containerd `v2.0.5-k3s2` returned
`failed to generate spec: failed to mkdir "": mkdir : no such file or directory` before Bash ran.

For the split-disk reproduction, use a scratch checkout of
[`61a8c5f0c5`](https://github.com/agentydragon/ducktape/tree/61a8c5f0c5e65248270262ed3281e1cc7a99b6db/agentplane/x/runner_layering_spike),
load its Nix devshell, and run on a host with `/dev/kvm`:

```bash
bb run //agentplane/x/runner_layering_spike:local_vm_probe
```

See the [original split-disk build](https://app.buildbuddy.io/invocation/868627f0-65b5-4c1f-8317-dfa291ae5009)
and [earlier combined-initramfs investigation](https://github.com/agentydragon/ducktape/blob/04feccb156864fdf057b6ac59cc17f198c3c72b4/agentplane/x/runner_layering/epistemic_state.md)
for the full probe history. The latter's 2.231 ± 0.066 s warm launch measurement includes the local
Bazel wrapper and used the earlier layout. The maintained [Firecracker guest](../../system_tests/firecracker/README.md)
and [KubeVirt guest](../../system_tests/vm/guest/BUILD.bazel) put their installed harnesses and
tools in the guest root and attach Runtime separately. Testing an independent tools disk on a maintained VM
remains part of the [production layering decision](../../plans/task_dag.md#runner_image_layering_design--what-carries-the-runner-the-harnesses-and-the-tools).
