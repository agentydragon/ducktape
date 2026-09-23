# Nix RBE Container Image TODOs

> This directory contains experimental image definitions. The published RBE
> container image is built from `devinfra/rbe_container_image/`; its workflow and digest
> pin live in `.github/workflows/rbe-container-image.yml` and
> `devinfra/image_pins.json`. Do not add a second publishing path here.

## Image Size

- Add back commented-out packages (Xvfb, D-Bus, cpio, Chromium deps) —
  currently excluded to reduce image size
- Removed permanently: QEMU, dosfstools, mtools, fuse3, fuse,
  gobject-introspection, cairo.dev, dbus.dev (unused)

## shiboken6 / PySide6

- `ezdxf[draw]` → `pyside6` → `shiboken6`/`pyside6-addons` fails to install on `bb remote`
- All 6.11.0 wheels exist on PyPI as `manylinux_2_34_x86_64` with `cp310-abi3`
- python-build-standalone reports glibc 2.40, `manylinux_2_34` is in supported tags
- `pip install --isolated wheel --no-deps pyside6-addons==6.11.0` **works** when
  run directly on the same image via `bb execute`
- But rules_python's `whl_installer` fails — pip only sees versions 6.8.0.2–6.9.3
- **Hypothesis**: stale Bazel repo rule cache or repository_cache on recycled
  `bb remote` runners. `--repository_cache=` didn't help, so it might be Bazel's
  internal repo rule cache (`external/` directory) on the recycled runner VM.
- This is a pre-existing issue on `origin/devel` too (not image-specific)

## Consider: Remove nixpkgs eval artifact

The nixpkgs source tree (~457MB) is retained in `/nix/store` because the Nix
profile manifest references it. The Dockerfile already uses `nix build` + GC
root instead of `nix profile install` to avoid pulling in profile metadata, but
nixpkgs is still retained as a transitive reference from the built closure.

Could be fixed by building the closure outside Docker and copying it in
(multi-stage build or CI-built closure tarball), so the final image layer only
contains the runtime closure without the nixpkgs evaluator artifact.

## Consider: Trim image further

Infrastructure tools not needed for CI builds are currently included:

- fluxcd (~111M)
- opentofu (~85M)
- helm (~75M)
- kubectl (~58M)
- kustomize, kubeconform, tflint, sops

Could save ~350M+ by splitting these into a separate `rbe-ci-tools` image or
Nix profile. Current image is ~3.6G (1.5G Ubuntu base + 2.2G Nix store); target
could be ~3G with infra tools removed.
