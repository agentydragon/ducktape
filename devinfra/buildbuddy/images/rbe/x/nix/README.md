# Nix RBE Container Image Experiment

Experimental Nix-based container image for BuildBuddy Remote Execution actions,
built with dockerTools.

The production RBE container image is in `devinfra/buildbuddy/images/rbe/Dockerfile`.
This is an alternative built with Nix instead of extending BuildBuddy's Ubuntu
executor image.

## Results of the Nix image experiment

This is an experimental image for both BuildBuddy's outer runner and selected
remote actions; it is not the production image. The 2026-11-17 experiment used
`ghcr.io/agentydragon/buildbuddy-remote-runner@sha256:bd4a60ca9384db24885f6b1f3d8cea5eb09d9830562237fea37037b832902fe6`.

- A [shell smoke test](https://app.buildbuddy.io/invocation/86d87f5a-2e94-4f5a-b6d0-ffec080255ac)
  passed on the self-hosted `kvm-vm` pool with Docker isolation using the
  earlier candidate `sha256:b99fbcdc6e793e763efaa429c8ac0dec3e2b36e973377d1e4ec6a7e8e82499d1`.
  Bash, Git, Python, GCC and Heaptrack started; Python reported glibc 2.42.
- A [Firecracker outer runner](https://app.buildbuddy.io/invocation/6399e14e-69af-4dd0-b1d9-4d669cce9842)
  started Bazel 9.2.0 after setting `NIX_LD=/share/nix-ld/lib/ld.so` and
  `NIX_LD_LIBRARY_PATH=/share/nix-ld/lib`. This was a version check without
  checkout, not a repository build on that runner.
- An [Excalidraw heap-profile test](https://app.buildbuddy.io/invocation/643066c7-23be-437d-be83-8b8e05b5a139)
  passed on `kvm-vm` with Docker isolation using the newer candidate. It used
  an uncommitted test-only image override, the loader environment above,
  optimized Rust binaries with debug info and frame pointers, and a copy of the
  analyzer executable as the profiling target. The test took 135 seconds;
  the heap-profiled analysis took 121 seconds. It produced a 400 MB compressed
  raw capture and the matching debug binary. Interpreting a sample resolved
  application symbols and source paths; some system libraries were unavailable
  on the interpretation host.

The 2026-11-17 loader probe rebuilt nix-ld with its fallback library path at
`/share/nix-ld/lib` and added `hostname`. With a fresh, empty `/run` and no
`NIX_LD` variables, the downloaded Bazel 9.2.0 ELF failed on the earlier image
(`libstdc++.so.6` not found) and printed `bazel 9.2.0` on the rebuilt image.
This was a local container compatibility check, not a Firecracker or pool test.

The image still needs work before regular CI use:

- Firecracker mounts a fresh `/run`, hiding nix-ld's standard fallback under
  `/run/current-system/sw/share/nix-ld`. The experimental loader now points
  directly at `/share/nix-ld/lib`; a Firecracker run without loader variables
  still needs validation.
- Cold image pulls exceeded the pool's five-minute limit; a later attempt
  succeeded after the cache warmed. Image size and distribution need attention.
- The first benchmark attempt lacked `prlimit`; adding `util-linux` fixed it.
  Heaptrack also emitted a nonfatal warning that `hostname` was missing;
  the rebuilt image includes it, pending a new profiler run.
- These are focused probes, not validation that this image supports the full
  CI toolchain, nested Docker, or all repository tests. No production image
  pin or workflow has been changed by the experiment.

The filesystem-based nix-ld fallback and FHS Bash used in a normal Nix-built
container are explained in <../../../../../debug/nixos_bazel_bash/README.md>
("Issue 4"). The separate Firecracker `/run` mount limits that approach here.

## NixOS BuildBuddy Remote Runner variant

The NixOS remote runner implementation lives in
[the remote runner experiment directory](../../../runner/x/nixos/README.md).

## Shared package list

The package list shared by both Nix container image experiments is in
[shared BuildBuddy Nix image package list](../../../nix_packages.nix).

## See also

- <devinfra/buildbuddy/images/rbe/Dockerfile> — the production Ubuntu-based RBE container image
- <devinfra/buildbuddy/docs/remote.md> — how `bb remote` and Firecracker work
- [TODO.md](TODO.md) — remaining work items
