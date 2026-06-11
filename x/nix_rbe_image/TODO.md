# Nix RBE Image TODOs

## CI & Publishing

- Build CI workflow to auto-build and push `nix-rbe-image` to GHCR
- Pin digest in `devinfra/image_pins.json` like the Ubuntu image
- Figure out rebuild/repin flow

## Image Size

- Add back commented-out packages (Xvfb, D-Bus, cpio, Chromium deps) —
  currently excluded to reduce image size
- Removed permanently: QEMU, dosfstools, mtools, fuse3, fuse,
  gobject-introspection, cairo.dev, dbus.dev (unused)

## `bb remote` Auto-Detection Issues

### Unpushed commit breaks auto-detect

When there are unpushed local commits, `bb remote` (without `--run_from_commit`
or `--run_from_branch`) tries to use the local HEAD SHA as the base commit. The
runner then does `git fetch --depth=1 origin <sha>` which fails with
`upload-pack: not our ref` because the commit doesn't exist on the remote.

**Questions to investigate**:

- How exactly does `getBaseBranchAndCommit` in `remotebazel.go` auto-detect?
  It should fall back to the default branch when the local commit isn't pushed.
  Why isn't it falling back?
- Does it check if the commit exists on the remote before using it?
- Is the issue that the local branch tracks a remote branch, so bb assumes the
  commit is pushed?
- `--run_from_branch=devel` works as a workaround but skips local patches
  (same footgun as `--run_from_commit`)
- What's the intended workflow for developing with unpushed commits?

### Docker in linux-sandbox

`--runner_exec_properties=init-dockerd=true` gives the runner VM a Docker daemon,
but Bazel's `linux-sandbox` blocks access.

**Socket mounting**: `--sandbox_add_mount_pair=/var/run/docker.sock` makes the
socket reachable, but `docker load` still fails with ENOENT on blob paths.

**Root cause found and fixed**: `tar.add()` on Bazel runfiles (which are
symlinks) records them as symlink entries with absolute target paths pointing
into the execroot. Docker extracts the tarball, creates symlinks, then tries to
follow them — but the absolute paths don't exist from the daemon's perspective.
Fix: `dereference=True` on `tarfile.open()` in `util/crane.py` to store file
content instead of symlinks.

No `--sandbox_add_mount_pair` or `no-sandbox` tag needed. Bazel's linux-sandbox
(non-hermetic mode, the default) inherits the entire host filesystem read-only.
Unix socket `connect()` works through read-only mounts (read-only only blocks
file creation/modification, not socket operations). So `/var/run/docker.sock` is
always accessible inside the sandbox.

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

## CI build workflow

Need a GitHub Actions workflow to auto-build and push the image on
Dockerfile/flake changes. Similar to existing `.github/workflows/rbe-image.yml`
but needs to handle Nix installation during Docker build. Should pin the
resulting digest in `devinfra/image_pins.json`.

## `bb remote` git sync

- Investigate how `bb remote` syncs local git state to the runner (the
  `getBaseBranchAndCommit` / patch generation flow in `remotebazel.go`).
- Currently: unpushed commits cause `git fetch --depth=1 origin <sha>` to fail
  with `not our ref`. Why doesn't it fall back to the default branch?
- `--run_from_commit=origin/devel` works but silently drops all local diffs
  (patches are only generated when both `--run_from_branch` and
  `--run_from_commit` are empty).
- This makes local iteration painful — you either push every change or lose
  your diff. There should be a better workflow.
