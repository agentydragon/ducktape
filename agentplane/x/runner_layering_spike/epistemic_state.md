# Epistemic State: Agentplane runner image layering

## Last updated: 2026-10-16 03:56 UTC

## 1. Objective

Determine whether an Agentplane runner can remain outside a separately updated tools filesystem
while a real pinned Codex process runs from a very small child root, and what prevents a faithful
local VM demonstration. The decision concerns packaging and process boundaries, not choosing a
production VMM. Required evidence is a real turn and native conversation resume, runner survival
after child termination, runner-private state outside the child, independently buildable tools and
boot artifacts, and a measured artifact size. Guest startup timing and a booted Firecracker guest
remain open while no compatible ELF `vmlinux` is available.

## 2. Available action space

Direct: BuildBuddy-backed Bazel build/test/query (`bbr`); local Bazel-run binaries using the pinned
devshell; inspect repository Nix, Bazel, Claude/Codex pins, init and runtime code; run the pinned
Firecracker binary with host KVM; exercise the runner against the repository's mock model/gateway;
inspect Bazel action graphs and produced layer sizes. The host supports unprivileged user/mount
namespaces, but RBE test actions may have different kernel restrictions.

User-mediated: operator can select an architecture after reviewing the evidence, authorize a
kernel build or an executor with namespaces if either is desired, or supply an existing compatible
ELF `vmlinux`. None is needed to complete the bounded current spike.

Acquirable: a Bazel-pinned ELF kernel output or a compatible existing artifact; a BuildBuddy test
execution environment that permits the required namespaces; and, only after a concrete Codex tool
failure, an additional pinned tools payload. No network install at runner boot is in scope.

## 3. Uncertainty register

Entropies below use stated discrete probabilities and are tagged `[VIBE]` because no empirical
reference class for this spike is available.

| ID  | Quantity                                                                                 | Prior / current estimate                         | Source                                                                                                                                 | Entropy            | Status                              |
| --- | ---------------------------------------------------------------------------------------- | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ----------------------------------- |
| U1  | Firecracker can boot with the host's current kernel                                      | 0; the exposed `bzImage` is not ELF              | Firecracker response below                                                                                                             | 0 bits             | resolved: no                        |
| U2  | Static Codex completes a full text turn from the sparse child root                       | yes                                              | RBE run reached `turn_completed` after procfs was mounted                                                                              | 0 bits             | resolved: yes                       |
| U3  | RBE permits a child user+mount namespace                                                 | 1.00 yes / 0.00 no                               | Clone-based RBE attempt entered namespace and mounted the root                                                                         | 0 bits             | resolved: yes                       |
| U4  | Ordinary Codex shell tools work from the sparse root with the Bash-only tools filesystem | 0.80 yes / 0.20 no `[VIBE]`                      | The first tool call failed at process creation with `No such file or directory (os error 2)`; root/path/closure cause is not diagnosed | 0.72 bits `[VIBE]` | open                                |
| U5  | Codex tools and boot layer are independently buildable                                   | 0.95 yes / 0.05 no `[VIBE]`                      | Separate Bazel targets and a local concatenated artifact built                                                                         | 0.29 bits `[VIBE]` | open: confirm final graph and sizes |
| U6  | Pinned Claude can use the same empty root without a runtime closure                      | 0.05 yes / 0.95 needs dynamic libraries `[VIBE]` | `ldd` shows a glibc loader and libraries                                                                                               | 0.29 bits `[VIBE]` | resolved: needs closure             |
| U7  | Debian and Nix environments can be consumed as tools roots without runner payload        | 0.70 yes / 0.30 no `[VIBE]`                      | Existing images and runner code indicate likely; no compatibility test                                                                 | 0.88 bits `[VIBE]` | open, follow-up only                |
| U8  | RBE permits mounting procfs from the new PID namespace                                   | yes                                              | Proc mount succeeded; Codex completed a full text turn                                                                                 | 0 bits             | resolved: yes                       |
| U9  | Codex history resumes after killing its process in the sparse root                       | yes                                              | RBE test confirmed resumed harness and prior assistant history                                                                         | 0 bits             | resolved: yes                       |

## 4. Hypothesis space

| ID      | Hypothesis                                                                                                                    | Probability   | Key evidence for                                                   | Key evidence against                                                                                | Distinguishing test                                                 |
| ------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| H1      | A minimal runner layer plus mounted tool roots is feasible; remaining issues are tool-layer contents and production packaging | 0.75 `[VIBE]` | Full Codex turn/resume and runner survival pass in child namespace | VM lacks ELF kernel; shell-tool launch failed with ENOENT                                           | Diagnose tool launch and independent artifact graph                 |
| H2      | Codex works, but basic coding requires a larger distro/Nix tools root                                                         | 0.15 `[VIBE]` | Existing runner image is Debian/Nix; Codex tool tests use shell    | Minimal turn/resume passes; first tool launch failure is not yet attributed to a missing dependency | Diagnose the `ENOENT`, then add only an observed missing dependency |
| H3      | Runner/harness coupling makes independent tools roots impractical                                                             | 0.05 `[VIBE]` | Codex home, workspace and process policy have coupling             | Full native resume passed                                                                           | Independent artifact graph and harness/tools split                  |
| H_other | Other packaging or kernel constraints dominate                                                                                | 0.05 `[VIBE]` | Guest boot not yet possible                                        | Full Codex turn/resume passed                                                                       | Update from the next failure or a boot                              |

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
   would fail. Pointer: local `bazelisk run //agentplane/x/runner_layering_spike:local_vm_probe` output.

3. **2026-10-16 03:00 UTC, first remote integration attempt.** Action: `bbr test
//agentplane/runner:test_restart`. Type: experiment. Cost: 101 s Bazel elapsed; 79.5 s test.
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
   experiment. Cost: 117.8 s Bazel elapsed; 105.1 s test. Raw result: `//agentplane/runner:test_restart`
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
   `44014602-68fe-4b86-90a7-00cfe9ecc376`, target `//agentplane/runner:test_restart`.

## 6. Current posterior state

- U1: Firecracker cannot boot the current `bzImage`; confidence 1.0 from the explicit ELF-loader
  rejection. A compatible kernel remains unavailable. No guest startup timing is measured.
- U2: resolved yes for a complete Codex text turn. The child root contains the pinned binary, procfs,
  a mounted workspace and session home, and tmpfs; the private sentinel check succeeded. Native
  process restart also passed.
- U3: resolved yes for the RBE test executor: clone flags created the user and mount namespaces and
  allowed the binds/tmpfs. The failed `unshare` path was a Go-runtime implementation issue, not a
  missing executor capability.
- U8: resolved yes with a child PID namespace; procfs allowed Codex to start and complete a turn.
- U9: resolved yes. The native Codex session resumed after child termination, and its prior user and
  assistant history was present in the next Responses request.
- U4: open. The first `exec_command` call from the sparse-root Codex session failed with `ENOENT`
  during process creation despite building the Bash-only tools filesystem. Do not infer the missing
  piece until the child executable path, cwd, environment, and extracted root layout are inspected.
- U5: separate build graph appears feasible; measured combined initramfs output was 287,253,784
  bytes locally. This is not a cold/warm time measurement and is not the exact standalone tools-root
  size. Confirm the standalone outputs and runfiles graph before making an independence claim.
- U6: pinned Claude needs a glibc loader and libraries according to `ldd`; no Claude child launch
  is tested. Do not share Codex's static-root claim with Claude.
- U7: Debian/Nix compatibility remains a later follow-up, after minimal dependency discovery.

## 7. Action queue

1. Trace the `ENOENT` from invocation `44014602-68fe-4b86-90a7-00cfe9ecc376` through Codex's child
   process request, environment and extracted tools root. Cost: bounded source/log inspection; first
   determine whether the failure is path/layout or an actual missing runtime dependency.
2. Correct only the demonstrated launch/rootfs issue, then rerun the same focused shell-tool test.
   Add another pinned payload only if the failure identifies a missing component.
3. Build standalone layer targets; inspect output sizes and Bazel dependency graph (type: `bbr
build`/query; low cost; confirms whether a tools update avoids boot-layer rebuild; estimates
   `[VIBE]`).
4. If an ELF kernel becomes available without introducing a kernel-build project, run local
   Firecracker and collect its existing boot timer output (type: local experiment; high information
   value, availability unknown). Do not add ad-hoc timing macros.
5. Close the spike with a recommendation and production DAG sequence; Debian/Nix compatibility is
   a follow-up only if it changes the choice.

## 8. Decision tree

```text
Clone-based RBE user+mount+PID namespaces and procfs work; turn and resume pass
└─ first exec_command fails with ENOENT
   └─ trace executable path, cwd, environment, and rootfs layout; add a payload only if needed

Compatible ELF vmlinux available?
├─ yes → local Firecracker boot probe, boot markers and built-in boot timer
└─ no → keep guest boot and startup timing explicitly unproven; do not build a kernel for this spike
```

## 9. Stopping criteria

Stop when the sparse-root runner test proves one completed turn, child exit, runner survival, resume,
and a shell-tool call; the separate artifact outputs and dependency graph establish whether tools and
boot layers can update independently; the unavailable kernel limitation and unmeasured boot timing
are recorded; and a recommendation names the remaining production gates. If RBE blocks namespace
creation and no authorized default-path local test can provide the same evidence, stop with that
exact visibility limit rather than claiming isolation or rebuilding the kernel.

## 10. Vibes ledger

- `[VIBE]` H1-H3 probabilities and entropy estimates: no calibration set for this repository-specific
  architecture decision. De-vibe with the real runner test and artifact dependency graph; load-bearing
  only until those tests run.
- `[VIBE]` probability that shell tooling is needed: inferred from existing Codex tool scenarios.
  The first concrete `exec_command` attempt returned ENOENT; de-vibe after tracing the launch path
  and repeating the tool call with the demonstrated fix.
- `[VIBE]` probability of native Codex history resuming after child restart: de-vibe with the next
  completed runner test.
- `[VIBE]` Debian/Nix compatibility estimate: not needed for the minimal-root decision. De-vibe only
  if the architecture review selects the layered runtime.
