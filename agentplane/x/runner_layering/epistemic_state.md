# Epistemic State: Agentplane runner image layering

## Last updated: 2026-10-16 05:28 UTC

## 1. Objective

Determine whether an Agentplane runner can remain outside a separately updated tools filesystem
while a real pinned Codex process runs from a very small child root. The decision concerns packaging
and process boundaries, not choosing a production VMM. Required evidence is a real turn and native
conversation resume, runner survival after child termination, runner-private state outside the child,
independently buildable tools and boot artifacts, a booted Firecracker guest, and artifact/startup
measurements. No production VMM or runtime layout is selected by this spike.

## 2. Available action space

Direct: BuildBuddy-backed Bazel build/test/query (`bbr`); local Bazel-run binaries using the pinned
devshell; inspect repository Nix, Bazel, Claude/Codex pins, init and runtime code; run the pinned
Firecracker binary with host KVM; exercise the runner against the repository's mock model/gateway;
inspect Bazel action graphs and produced layer sizes. The host supports unprivileged user/mount
namespaces, but RBE test actions may have different kernel restrictions.

User-mediated: operator selects an architecture after reviewing the evidence. No kernel build,
cluster mutation, or production cutover is part of this spike.

Acquired: a digest-pinned Firecracker CI ELF kernel and a BuildBuddy executor that permits the
required namespaces. No network install at runner boot is in scope.

## 3. Uncertainty register

Entropies below use stated discrete probabilities and are tagged `[VIBE]` because no empirical
reference class for this spike is available.

| ID  | Quantity                                                                                 | Prior / current estimate                                             | Source                                                                                                              | Entropy            | Status                                                 |
| --- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------ |
| U1  | Firecracker can boot the pinned ELF kernel in this environment                           | yes                                                                  | Local Firecracker 1.12.1 booted Linux 6.1.128 and ran the pinned Codex payload                                      | 0 bits             | resolved: yes                                          |
| U2  | Static Codex completes a full text turn from the sparse child root                       | yes                                                                  | RBE run reached `turn_completed` after procfs was mounted                                                           | 0 bits             | resolved: yes                                          |
| U3  | RBE permits a child user+mount namespace                                                 | 1.00 yes / 0.00 no                                                   | Clone-based RBE attempt entered namespace and mounted the root                                                      | 0 bits             | resolved: yes                                          |
| U4  | Ordinary Codex shell tools work from the sparse root with the Bash-only tools filesystem | yes                                                                  | Focused RBE scenario executed `printf SPARSE_ROOT_TOOL_OK` through Codex's shell tool                               | 0 bits             | resolved: yes                                          |
| U5  | Codex harness, guest boot, and tools artifacts build separately                          | yes; no dependency path from VM initramfs to tools-root target       | Separate Bazel targets; 1.9 MB initramfs base, 22.3 MB tools OCI layer, 287.8 MB combined local initramfs           | n/a                | target graph resolved; rebuild isolation not exercised |
| U6  | Pinned Claude can use the same empty root without a runtime closure                      | 0.05 yes / 0.95 needs dynamic libraries `[VIBE]`                     | `ldd` shows a glibc loader and libraries                                                                            | 0.29 bits `[VIBE]` | resolved: needs closure                                |
| U7  | Debian and Nix environments can be consumed as tools roots without runner payload        | 0.70 yes / 0.30 no `[VIBE]`                                          | Existing images and runner code indicate likely; no compatibility test                                              | 0.88 bits `[VIBE]` | open, follow-up only                                   |
| U8  | RBE permits mounting procfs from the new PID namespace                                   | yes                                                                  | Proc mount succeeded; Codex completed a full text turn                                                              | 0 bits             | resolved: yes                                          |
| U9  | Codex history resumes after killing its process in the sparse root                       | yes                                                                  | RBE test confirmed resumed harness and prior assistant history                                                      | 0 bits             | resolved: yes                                          |
| U10 | Guest boot and layer-preparation time                                                    | warm preparation 5.6 s; repeated end-to-end launches 2.231 ± 0.066 s | BuildBuddy cached build; three fresh guest launches via `perf stat`; first local initramfs materialization 24.776 s | open               | guest-only and cache-cold timing                       |

## 4. Hypothesis space

| ID  | Hypothesis                                                                                    | Probability                        | Key evidence for                                                              | Key evidence against                                        | Distinguishing test                                                  |
| --- | --------------------------------------------------------------------------------------------- | ---------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------- | -------------------------------------------------------------------- |
| H1  | A minimal runner layer plus mounted tool roots is feasible; production packaging remains open | supported by current prototype     | Full Codex turn/resume, Bash tool, runner survival, and local guest boot pass | Production packaging and Claude remain untested             | Measure update boundaries and evaluate remaining compatibility cases |
| H2  | Basic Codex tool use requires a larger distro/Nix tools root                                  | contradicted for one shell command | Existing runner image is Debian/Nix                                           | Bash-only tools root successfully ran a Codex shell command | Broader coding task remains untested                                 |
| H3  | Runner/harness coupling makes independent tools roots impractical                             | not supported by this evidence     | Process and state paths require explicit mounts                               | Harness/runner lifecycle and Bash tools are separate inputs | Verify artifact rebuild isolation                                    |

Key distinguishing test: a complete runner session through the sparse root, including a killed
Codex process and a resumed turn, with the child unable to read a runner-only sentinel.

## 5. Evidence log

1. **2026-10-15, local capability and payload inspection.** Action: inspect `/dev/kvm`, host kernel
   config, pinned binaries and run the static Codex binary from an empty chroot under an unprivileged
   user/mount namespace. Type: experiment/observation. Cost: local investigation. Raw results from
   the terminal: `/dev/kvm` was readable and writable; `unshare --user --map-root-user chroot ...
/opt/harnesses/codex --version` succeeded; Codex printed `codex-cli 0.157.0`. The Codex binary
   is static-pie, size 285,340,072 bytes. Claude 2.1.252 requires the ELF interpreter
   `/lib64/ld-linux-x86-64.so.2` and the glibc libraries reported by `ldd`. Update: this supports
   H1 for Codex and resolves U6; it does not prove app-server operation or tool use. Pointer: pinned
   Bazel runfiles and local process probe in this worktree; exact outputs summarized in the task
   transcript.

2. **2026-10-15, Firecracker boot probe.** Action: build and run pinned Firecracker 1.12.1 with the
   host KVM and current system kernel. Type: experiment. Cost: one Bazel build and local VM launch.
   Raw result: Firecracker accepted the API configuration and reached `InstanceStart`, then
   returned `Kernel Loader: failed to load ELF kernel image: ... Invalid Elf magic number`. The
   current kernel path contains a compressed `bzImage`; this checkout has no `vmlinux` output.
   Update: U1 resolved to “cannot boot this host kernel”; do not infer that a different ELF kernel
   would fail. Pointer: local `bazelisk run //agentplane/x/runner_layering:local_vm_probe` output.

3. **2026-10-16 03:00 UTC, first remote integration attempt.** Action: `bbr test
//agentplane/env/e2e:test_restart`. Type: experiment. Cost: 101 s Bazel elapsed; 79.5 s test.
   Raw result: 24 tests passed and the new test failed before session launch because the Go executable
   runfile path was `.../child_exec_/child_exec`, not `.../child_exec`. Update: no process-boundary
   likelihood update. Pointer: BuildBuddy invocation
   `8eab1c98-eb9f-40d7-9ad9-eeda5c5431e9`, target log.

4. **2026-10-16 03:03 UTC, corrected remote integration attempt.** Action: rerun the same target after
   fixing the runfile path. Type: experiment. Cost: 51 s Bazel elapsed; 48.5 s test. Raw result: 24
   tests passed; namespace setup failed with exact stderr `runner-layering child: unshare user and
mount namespaces: invalid argument`. Update: the first implementation called `unshare` from a
   Go runtime process; this does not distinguish a runtime threading restriction from a remote
   kernel/seccomp restriction. Replace it with a child process created using namespace clone flags
   and test again. Pointer: BuildBuddy invocation `f992d0f6-7018-4cbf-8422-62af9d31ac3b`, target log.

5. **2026-10-16 03:08 UTC, clone-based retry.** Action: create a second stage with
   `CLONE_NEWUSER|CLONE_NEWNS`, ID mappings and sparse-root mounts, then run the RBE test. Type:
   experiment. Cost: 140 s Bazel elapsed; 122.7 s test. Raw result: the wrapper printed
   `runner-layering: runner-private state remains outside child root`; Codex then printed
   `WARNING: proceeding, even though we could not create PATH aliases: no /proc/self/exe available.
Is /proc mounted?` and failed with `Error: Codex executable path is not configured`. Update: the
   earlier `unshare` failure was caused by using `unshare` from the Go runtime; clone flags work on
   the RBE executor. The private-state check passed. Codex requires a minimal `proc` mount to locate
   its executable; add that one pseudo-filesystem and retry. Pointer: BuildBuddy invocation
   `788e4f1c-28db-466e-9c26-b68de97ebcc8`, target log.

6. **2026-10-16 03:11 UTC, proc mount attempt.** Action: mount `proc` in the clone-created mount
   namespace and rerun. Type: experiment. Cost: 104 s Bazel elapsed; 91.8 s test. Raw result:
   `runner-layering child: mount proc on .../child-root/proc: operation not permitted`. The RBE
   invocation predates the `CLONE_NEWPID` change, so it did not yet have a PID namespace. Update:
   do not conclude that the executor blocks a correctly namespaced proc mount; retry with the
   planned user, mount and PID namespace flags. Pointer: BuildBuddy invocation
   `8342f4e4-c557-4961-bf37-a3cede5b96ee`, target log.

7. **2026-10-16 03:15 UTC, PID-namespaced procfs and Codex turn.** Action: rerun with
   `CLONE_NEWUSER|CLONE_NEWNS|CLONE_NEWPID` and mount procfs. Type: experiment. Cost: 178 s Bazel
   elapsed; 171.2 s test. Raw result: sparse-root Codex completed the scripted first text turn; the
   test then timed out waiting for its fence diagnostic after the cursor had advanced through that
   event to `turn_completed`. The wrapper exits before Codex starts if the runner-private sentinel
   is visible, so the completed turn also proves that check succeeded. Update: full Codex text turn,
   private-state separation, and procfs are now demonstrated. Fix the assertion to search entries
   already read, then rerun to verify runner survival and native resume. Pointer: BuildBuddy
   invocation `2c1f9517-c91b-4b9d-9581-1b774c9aece1`, target log.

8. **2026-10-16 03:20 UTC, corrected child-exit and resume path.** Action: observe
   `harness_exited`, confirm runner liveness, reattach, and resume the same Codex session. Type:
   experiment. Cost: 117.8 s Bazel elapsed; 105.1 s test. Raw result: `//agentplane/env/e2e:test_restart`
   passed; the test confirmed the sparse-root text turn, sentinel isolation, child exit without
   runner exit, `harness_started.resumed`, and that the next model request contained both the first
   and second user inputs plus prior assistant text. Update: U9 resolved yes. Pointer: BuildBuddy
   invocation `5e12baac-ba8a-45a9-8491-b1fdba124056`.

9. **2026-10-16 03:21 UTC, shell tool probe.** Action: extend the same resumed session with one real
   `exec_command` that runs `printf SPARSE_ROOT_TOOL_OK`, with the separately built Bash-only tools
   root extracted into the child root. Type: experiment. Cost: 78.5 s test time. Raw result: the
   `tools_rootfs` dependency built, but Codex returned `exec_command failed: CreateProcess { message:
"Rejected(\"Failed to create unified exec process: No such file or directory (os error 2)\")"
}`. The assertion failed and teardown reported one unsettled model exchange. This proves only that
   the current shell-tool launch path is broken; it does not yet distinguish a missing executable or
   runtime file from a path, working-directory, or rootfs layout error. Update: U4 remains open; trace
   the concrete child process setup before adding another dependency. Pointer: BuildBuddy invocation
   `44014602-68fe-4b86-90a7-00cfe9ecc376`, target `//agentplane/env/e2e:test_restart`.

10. **2026-10-16 05:12 UTC, Codex shell tool and logical cwd.** Action: keep the runner's host
    workspace as the bind source while setting the persisted child `SessionSpec.cwd` to `/workspace`,
    the path visible inside the chroot; retain `/dev/null` in the child device mount. Type: remote
    integration experiment. Raw result: the focused test passed, including the `printf
SPARSE_ROOT_TOOL_OK` shell tool call, child termination, runner survival, conversation resume,
    and private-state check. The direct Bash probe passed after mounting `/dev/null`; the Codex tool
    call still failed until its logical cwd matched the mounted path. Update: U4 resolved yes for this
    pinned Codex and Bash root. Pointer: BuildBuddy invocation
    `d2e3808c-ad70-47ff-ad10-23868996d2c2`, target `//agentplane/env/e2e:test_restart`.

11. **2026-10-16 05:12 UTC, pinned Firecracker guest boot.** Action: pin the official Firecracker
    1.12 CI `vmlinux-6.1.128` ELF kernel (`sha256
27a8310b9a727517e9eb02044524b6ceb77de5728e3491b6974d5c846227ecc8`), make nested CPIO parent
    directories explicit, and launch Firecracker 1.12.1 on host KVM. Type: local VM experiment.
    Raw result: 512 MiB reached the guest but could not unpack the 275 MiB initramfs; at 1 GiB,
    Linux ran `/init`, Codex printed `codex-cli 0.157.0` in the sparse child chroot, and guest init
    remained alive after Codex exited. Run outside the sandbox to access `/dev/kvm`. Update: U1
    resolved yes for this pinned kernel/VMM pair. The kernel is an experiment input, not a production
    choice. Pointer: successful `bb run //agentplane/x/runner_layering:local_vm_probe`,
    BuildBuddy invocation `f63ca51e-cdee-43d8-8670-1f2f46f6d339`.

12. **2026-10-16 05:15 UTC, artifact sizes and build.** Action: build the independent base-initramfs
    and tools-rootfs targets. Type: BuildBuddy-backed build. Raw result: base initramfs 1,913,348
    bytes; Bash tools OCI layer tar 22,277,632 bytes; combined local VM initramfs 287,783,968
    bytes, including the 285,340,072-byte Codex binary and bundled bwrap. A warm two-target build
    completed in 5.577 s (3.90 s critical path; 19 action-cache hits, 4 remote-cache hits, 1 remote
    action). Update: artifacts are separately buildable and sizes are measured; cold build and
    repeatable startup timings remain open. Pointer: BuildBuddy invocation
    `075cdcc5-ce18-402c-89b9-095b18cad1a8`.

13. **2026-10-16 05:20 UTC, final guest and runner checks.** Action: rerun the VM probe after removing
    temporary diagnostics, run the focused Codex shell/resume integration again, query the target
    graph, and profile three local VM launches. Type: local and RBE experiments. Raw result: local
    Firecracker boot passed; the focused RBE test passed in 30.9 s; Bazel `somepath` from
    `local_vm_initramfs` to `tools_rootfs` returned an empty result. `perf stat --repeat 3 -- bb run
//agentplane/x/runner_layering:local_vm_probe` measured 2.231 ± 0.066 s elapsed (1.716 s
    task-clock) across three fresh guest boots with warm host/build artifacts. This includes the
    Bazel local-run wrapper, not just guest boot. Update: target graph separation is established;
    distinct tools mounting in the guest and cache-cold packaging remain open. Pointers: local VM
    invocation `b7123dd3-7fcd-44e1-8777-10509293a7df`; RBE test invocation
    `fc1dfd61-2b3f-45f2-970a-74902a4a8f99`; query invocation
    `0a961269-96dd-48b3-968f-956ef73ab61b`; profiler runs `11ba1168-553d-4bb9-b655-b9f184fed573`,
    `4001e8d1-565c-453e-86f9-8ced0ecdefa8`, and `ba37273f-14b8-4363-937c-f79fa9391131`.

14. **2026-10-16 05:28 UTC, rebased-head verification.** Action: rebase the spike on devel
    `3f8379123a`, rerun the focused Codex child test and boot the local VM from that tree. Type: RBE
    and local experiments. Raw result: `//agentplane/env/e2e:test_restart` passed in 27.6 s; local
    Firecracker again ran Codex 0.157.0 and left init alive. Bazel analysis emitted 404 warnings for
    some pinned Debian snapshot index URLs but resolved the tools target and completed the test.
    Update: the current devel base preserves the tested behavior. Pointers: test invocation
    `d62754ca-1882-405f-b310-b0ead71740d8`; local VM invocation
    `b692c329-47ad-4623-a9f3-6c2b72555bf9`.

## 6. Current posterior state

- U1: resolved yes for the pinned Firecracker 1.12.1 plus official 6.1.128 ELF kernel; the guest
  booted on host KVM and ran the pinned Codex payload.
- U2/U3/U8/U9: resolved yes in the RBE runner test. Codex completed a text turn, the child namespace
  mounted `/proc`, workspace, Codex home, `/dev/null`, and tmpfs, runner-private state stayed out of
  the child root, and conversation state resumed after killing the harness.
- U4: resolved yes for a real Bash shell call in the sparse child. The cause of the earlier ENOENT
  was the persisted logical cwd being a host path absent inside the chroot; `/dev/null` was also
  necessary for the Bash preflight.
- U5: separate Bazel targets build, and `somepath(local_vm_initramfs, tools_rootfs)` is empty.
  Current outputs are 1,913,348-byte base initramfs, 22,277,632-byte Bash OCI layer tar, and
  287,783,968-byte local VM initramfs. Actual independent-update behavior in a production packaging
  path is not implemented.
- U6: pinned Claude's `ldd` output requires a glibc loader and libraries; no Claude child launch is
  tested. Do not extend Codex's static-root result to Claude.
- U7: Debian/Nix compatibility remains a follow-up after this minimal dependency experiment.
- U10: warm BuildBuddy preparation of the base and tools targets took 5.577 s total (3.90 s critical
  path). Three fresh guest launches with warm host/build artifacts averaged 2.231 ± 0.066 s through
  `bb run`; the CLI and wrapper are included. A cache-cold build and guest-only latency remain
  unmeasured. Two attempted cache-bypass builds still reported action-cache hits, so they are not
  classified as cold.

## 7. Action queue

1. Verify the Bazel dependency graph has no tools-rootfs edge into the base or boot initramfs and
   record independent rebuild behavior. Add no compatibility alias or second image mechanism.
2. Use the existing profiler on repeated local VM launches and, if practical, a cache-cold packaging
   run. Report measured boundaries; do not add timing instrumentation to the guest.
3. Close the spike with an evidence-based recommendation and production DAG sequence. Claude,
   conventional Debian/Nix compatibility, and a full coding task remain explicit follow-ups rather
   than implied support.

## 8. Decision tree

```text
Clone-based RBE namespaces, Codex turn/resume, runner-state fence, and Bash shell tool pass.
Local Firecracker boots the pinned ELF kernel and Codex payload.
└─ Remaining: verify independent artifact rebuilds and measure warm/cold preparation/startup.
```

## 9. Stopping criteria

Stop when the sparse-root runner test proves one completed turn, child exit, runner survival, resume,
and a shell-tool call; the local guest boot is real; separate outputs and their dependency graph
establish the update boundary; measurements name what they cover; and a recommendation names the
remaining production gates. Do not claim complete isolation, arbitrary-rootfs support, or a production
cutover from this prototype.

## 10. Vibes ledger

- `[VIBE]` probability that a full coding task, Claude child, or conventional Nix root works: those
  cases are not tested and are not needed to establish minimal Codex plus Bash feasibility.
- No calibrated probability is assigned to cache-cold build timing, Claude startup or broad
  compatibility; those remain empirical follow-ups.
