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
On the self-hosted `kvm-vm` pool, a [digest-pinned action test](https://app.buildbuddy.io/invocation/762003d2-b9d6-43c4-b1a1-c1d3b0dc261f)
ran a dynamically linked C++ executable with an empty environment and printed
`fhs-loader-ok`; `hostname` also ran, and the test confirmed the standard
`/run/current-system/sw/share/nix-ld` path was absent. This was a Docker-isolated
pool action, not a Firecracker runner test. Its image was
`ghcr.io/agentydragon/buildbuddy-remote-runner@sha256:7ff3661d85a9245eb5306a0423e433c3d0997f71607b31a982baa5e224601f6b`.

The [Firecracker sudo/run-under probe](https://app.buildbuddy.io/invocation/b057170b-c942-4314-8ffc-4cdcd6e880bf)
used candidate
`ghcr.io/agentydragon/buildbuddy-remote-runner@sha256:36021aecc9bc48aaf74748a7e0eb9aef8fe966fa0678443e10e7cec99e822ea0`,
published by [this candidate workflow](https://github.com/agentydragon/ducktape/actions/runs/44123299486).
It ran with `uid=1000`, `init-dockerd=true`, and `recycle-runner=false`; the unchanged
`.bbrc` clocksource wrapper succeeded, `sudo -n id -u` returned `0`, and a nested sudo
attempt as `nobody` was denied. The test also confirmed
`/run/current-system/sw/share/nix-ld` was absent
and Bazel 9.2.0 ran under `env -i`. It did not check out the repository or build a repository
target. A separate [Firecracker repository run](https://app.buildbuddy.io/invocation/459ae447-3ee3-4585-8683-b522d4af6d34)
checked out exact `devel` commit `5fee893d9aeb9c20e87a55339c29128bc0e92fd1` with this candidate
as the outer runner and the unchanged `.bbrc` wrapper. Its [child Bazel invocation](https://app.buildbuddy.io/invocation/c1715a45-40e1-4b4e-a757-9fb196acd0f6)
passed uncached `//devinfra/js/debundle/perf:excalidraw_pipeline_test` (2.4 seconds in
TestRunner; 43.3 seconds for the Bazel invocation). The child action used the production-pinned
action image on `kvm-vm` with Docker isolation, so this validates the candidate as an outer
Firecracker runner and the run-under wiring, not the Nix image as an inner action image.

An initial [nested Docker probe](https://app.buildbuddy.io/invocation/74dd8a49-bda5-426f-8e1c-ffe7bed27195)
on that candidate found `/var/run` was a root-owned `0700` directory, blocking buildbuddy from
reaching dockerd's `root:docker` socket even though buildbuddy belonged to the docker group.
The [production runner probe](https://app.buildbuddy.io/invocation/52db903a-3fcf-4797-a2da-0f04252b709d)
showed the standard `/var/run` symlink layout, with Docker socket mode `0660`; a user outside
the docker group was denied by the socket. The Nix image now matches that layout by linking
`/var/run` to `/run`.

The [updated nested Docker probe](https://app.buildbuddy.io/invocation/da9944cd-7042-4829-9caa-28170c3eaf2f)
used candidate
`ghcr.io/agentydragon/buildbuddy-remote-runner@sha256:a27f07792ef8580daf05eaa6303e8a83f7aae91e998a162887ea8ced788f17b7`,
published by [this candidate workflow](https://github.com/agentydragon/ducktape/actions/runs/44130000640).
With the unchanged `.bbrc` Firecracker settings and `init-dockerd=true`, it saw `/var/run -> /run`
and the mounted `/run` directory as `root:root` mode `1777`. The socket was `root:docker` GID
131 mode `0660`; `docker info` succeeded as `uid=1000(buildbuddy)`. Running the same command
as `nobody` was denied by socket permissions. The probe pulled BusyBox 1.36.1, started a unique
container, observed `nested-docker-ok` and exit status 0, and removed that container by ID.
This validates daemon access and one container start/remove path, not `docker build`, volume
mounts, multi-container workloads, or full CI.

The image still needs work before regular CI use:

- Cold image pulls exceeded the pool's five-minute limit; a later attempt
  succeeded after the cache warmed. The new candidate repeated this pattern:
  the [first action attempt](https://app.buildbuddy.io/invocation/55e4c6ac-fd80-4988-8902-52332249b0dd)
  timed out while pulling the image, then the retry ran inside it. Image size
  and distribution need attention.
- The first benchmark attempt lacked `prlimit`; adding `util-linux` fixed it.
  Heaptrack also emitted a nonfatal warning that `hostname` was missing;
  the rebuilt image includes it, but the Nix image still needs its own profiler
  action run.
- These focused probes do not validate the full CI toolchain, all repository
  tests, `docker build`, volume mounts, or multi-container workloads. No production
  image pin or default workflow has been changed by the experiment.

The filesystem-based nix-ld fallback and FHS Bash used in a normal Nix-built
container are explained in <../../../../../debug/nixos_bazel_bash/README.md>
("Issue 4"). The separate Firecracker `/run` mount is why this image uses its
own `/share/nix-ld/lib` fallback.

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
