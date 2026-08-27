# Attic Container: `path escapes from parent` on NixOS Workers

**Date**: 2026-03-23
**Status**: Fixed — 2.2.3 overlay in k8s-worker.nix; pending nixos-25.11 channel update so overlay can be removed

## Symptom

Attic pod (`ghcr.io/zhaofengli/attic:latest`) fails on NixOS workers with:

```
Error: failed to create containerd container: mount callback failed on
  /var/lib/containerd/tmpmounts/containerd-mount...:
  openat etc/passwd: path escapes from parent
```

Works fine on Talos nodes (containerd 2.1.6, Go 1.23).

## Root Cause

Three things intersect:

1. **Go 1.24.0** (Feb 2025) tightened `os.DirFS` as part of introducing the new
   `os.Root` traversal-resistant file API. Absolute symlinks are now **rejected by
   design** — `DirFS.Open` sees a symlink target starting with `/` and treats it as
   a path escape, even if the target exists inside the DirFS root. This is intentional
   security hardening, not a Go bug
   ([Go blog](https://go.dev/blog/osroot),
   [#75335 — closed as expected behavior](https://github.com/golang/go/issues/75335)).

2. **NixOS container images** symlink `/etc/passwd` → `/nix/store/...` (absolute
   path). Most distro images have a regular file at `/etc/passwd`, so they're
   unaffected.

3. **containerd's `UserFromFS`** (in `pkg/oci/spec_opts.go`) resolves UIDs by
   calling `root.Open("etc/passwd")` where `root` is an `os.DirFS` of the container
   rootfs. When the pod spec sets `runAsUser` (like our attic deployment does),
   containerd hits this code path before the container starts. With Go 1.24, the
   `Open` call fails on the NixOS absolute symlink → `CreateContainerError`.

The bug is in **containerd** — it doesn't handle Go 1.24's stricter `DirFS` behavior
for absolute symlinks.

- **containerd issue**: <https://github.com/containerd/containerd/issues/12683>
- **Fix**: PR [#12732](https://github.com/containerd/containerd/pull/12732) — adds
  `openUserFile` helper that catches the `Open` failure, reads the symlink target
  with `Readlink`, strips the leading `/` to make it relative, and retries.

## Fix in containerd 2.2.3

PR #12732 was merged to `main` on 2026-01-14. It was cherry-picked to `release/2.2`
via [PR #13015](https://github.com/containerd/containerd/pull/13015), merge commit
`66751400b1249f624231cd439c5927ac22a3a8db`. A follow-up ([`ee4179e5`](https://github.com/containerd/containerd/commit/ee4179e5))
extended the fix to `/etc/group` as well. Both fixes shipped in **v2.2.3** (not v2.2.2 —
v2.2.2 was tagged on 2026-03-10, two days before the cherry-pick landed on 2026-03-12).

## Current State (2026-05-15)

- `k8s-worker.nix` overlay pins containerd to 2.2.3 (fixes both /etc/passwd and /etc/group)
- nixos-25.11 still ships 2.2.1 — overlay still needed
- nixos-unstable has 2.2.3 — overlay can be removed once 25.11 catches up
- Wyrm2 host-specific overlay removed (was needed when shared overlay used v2.2.2)

## Affected Nodes

| Node           | containerd      | Go   | Affected? |
| -------------- | --------------- | ---- | --------- |
| talos-vps-cp-0 | 2.1.6           | 1.23 | No        |
| talos-vps-cp-1 | 2.1.6           | 1.23 | No        |
| talos-pve-cp-0 | 2.1.6           | 1.23 | No        |
| wyrm2          | 2.2.1 (nixpkgs) | 1.24 | **Yes**   |
| rugged         | 2.2.1 (nixpkgs) | 1.24 | **Yes**   |

## Next Steps

- [ ] Once nixos-25.11 bumps containerd to ≥2.2.3, remove the overlay from `k8s-worker.nix`
