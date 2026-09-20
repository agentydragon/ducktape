# Nix RBE Image Experiment

Experimental Nix-based BuildBuddy RBE worker image built with dockerTools.

**The primary (working) approach** is in `devinfra/rbe_image/Dockerfile` — the
Ubuntu base image with Nix devtools baked in via `nix build .#rbetools`.

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
<../../cluster/k8s/haku/workspaces/image/README.md>; mechanism in
<../../debug/nixos*bazel_bash/README.md> "Issue 4"). Whether that also revives \_this*
image under Firecracker is untested — goinit's pivot-root is a separate question from the
loader — but the stated blocker for the dockerTools variant no longer holds as written.

## NixOS container variant

The NixOS worker implementation now lives in
[the sibling worker directory](../nix_rbe_worker/README.md), with its module,
flake outputs, and shared package list together.

## Shared package list

The worker package list used by this image is in
[the worker directory](../nix_rbe_worker/packages.nix).

## See also

- <devinfra/rbe_image/Dockerfile> — the working Ubuntu+Nix approach
- <devinfra/docs/bb_remote_internals.md> — how `bb remote` and Firecracker work
- [TODO.md](TODO.md) — remaining work items
