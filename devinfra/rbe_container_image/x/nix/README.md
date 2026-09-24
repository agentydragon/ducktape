# Nix RBE Container Image Experiment

Experimental Nix-based container image for BuildBuddy Remote Execution actions,
built with dockerTools.

The production RBE container image is in `devinfra/rbe_container_image/Dockerfile`.
This is an alternative built with Nix instead of extending BuildBuddy's Ubuntu
executor image.

This directory contains an alternative that was explored but has limitations:

## `default.nix` — dockerTools.buildLayeredImage

Plain Docker image built entirely from Nix closures. No systemd, no FUSE.

**Limitation**: NixOS glibc has nix-store paths compiled in for library search.
Dynamically-linked binaries downloaded at runtime (Bazel from bazelisk,
python-build-standalone) can't find `libstdc++.so.6` without `LD_LIBRARY_PATH`
or nix-ld — and BuildBuddy's goinit doesn't set these env vars.

**Update 2026-07-26 — "doesn't set these env vars" is no longer fatal.** nix-ld does not
need environment variables: it has compiled-in defaults under
`/run/current-system/sw/share/nix-ld` and works with an empty environment once that
directory exists in the image. A dockerTools image built that way now runs
`bazel test //...` at 25/26 (the Haku sandbox image — see
<../../../../cluster/k8s/haku/workspaces/image/README.md>; mechanism in
<../../../debug/nixos_bazel_bash/README.md> "Issue 4"). Whether that also revives \_this*
image under Firecracker is untested — goinit's pivot-root is a separate question from the
loader — but the stated blocker for the dockerTools variant no longer holds as written.

## NixOS BuildBuddy Remote Runner variant

The NixOS remote runner implementation lives in
[the remote runner experiment directory](../../../buildbuddy_remote_runner/x/nixos/README.md).

## Shared package list

The package list shared by both Nix container image experiments is in
[shared BuildBuddy Nix image package list](../../../buildbuddy_nix_image_packages.nix).

## See also

- <devinfra/rbe_container_image/Dockerfile> — the production Ubuntu-based RBE container image
- <devinfra/docs/bb_remote_internals.md> — how `bb remote` and Firecracker work
- [TODO.md](TODO.md) — remaining work items
