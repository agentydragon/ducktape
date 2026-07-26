# Nix RBE Image Experiments

Experimental approaches for Nix-based BuildBuddy RBE worker images.

**The primary (working) approach** is in `devinfra/rbe_image/Dockerfile` — the
Ubuntu base image with Nix devtools baked in via `nix build .#rbetools`.

This directory contains two alternative approaches that were explored but have
limitations:

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

## `nixos.nix` — NixOS container (docker-image.nix)

Full NixOS container with systemd, envfs, nix-ld. Would handle all FHS
compatibility automatically.

**Limitation**: BuildBuddy's Firecracker goinit does NOT run the container's
`/init` — it pivot-roots into the rootfs and spawns its own vmexec service.
systemd never starts, so envfs/nix-ld activation scripts don't run.

## `packages.nix` — Shared package list

Shared between both experimental images and potentially the Dockerfile approach.

## See also

- <devinfra/rbe_image/Dockerfile> — the working Ubuntu+Nix approach
- <devinfra/docs/bb_remote_internals.md> — how `bb remote` and Firecracker work
- <x/nix_rbe_image/TODO.md> — remaining work items
