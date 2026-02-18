# Current Plan

## Build Status

**Diff Summary**: 225 real differences (build v18, 2026-02-18)

| Category       | Count   |
| -------------- | ------- |
| Identical      | 120,691 |
| Excluded       | 496,932 |
| **Real diffs** | **225** |

## Real Difference Analysis

### Only in built (183 — primary issue: Podman package)

The live container has Docker CE (`docker-ce`, `containerd.io`, etc.) but does
**NOT** have Podman or any of its related packages. The Dockerfile currently
installs: `podman buildah crun conmon catatonit fuse-overlayfs fuse3 slirp4netns
passt netavark aardvark-dns uidmap containernetworking-plugins`. None of these
exist in the live container. This accounts for 183 of 225 real differences:

- 63 `/usr/share/doc/` entries for Podman packages
- 43 other files (`/sbin.usr-is-merged`, `/usr/lib/cni/*`, `/usr/lib/podman/*`,
  systemd units, `/usr/libexec/podman/*`)
- 24 `/etc/` entries (apparmor, CNI config, `/etc/containers/*`, fuse.conf, systemd)
- 20 system binaries (podman, buildah, crun, conmon, fuse-overlayfs, passt, etc.)
- 15 `/var/lib/systemd/deb-systemd-helper-enabled/` (podman service enablement)
- 11 `/usr/share/` (bash completions, containers.conf, seccomp.json, etc.)
- 7 system libs (libfuse3, libslirp, libsubid, libcrun.a)

**Fix**: Remove all Podman-family packages from the Dockerfile apt install.

### Only in live (35 — all session-specific, expected)

- 9 `/etc/ssl/certs/mkcert_*` and hash symlinks: session-specific dev CAs
- 5 `/usr/local/share/ca-certificates/mkcert_*`: same mkcert CAs
- 18 `/root/.docker/*`: Docker BuildKit/buildx state from running builds this session
- 3 `/opt/containerd/{bin,lib}`: Docker daemon installs containerd plugins here
- 1 `/root/.zshrc`: live container has a zsh config; built doesn't

These are all expected runtime state, not gaps in the Dockerfile.

### Content changed (7)

- `/usr/local/bin/environment-manager` — binary was updated between capture runs
- `/etc/ssl/certs/ca-certificates.crt` — live has session mkcert CAs appended
- `/var/lib/dpkg/status` — built has Podman packages, live doesn't
- `/usr/lib/x86_64-linux-gnu/libpng16.so.16.43.0` — same size, hash differs (minor)
- `/usr/lib/x86_64-linux-gnu/libpng16.a` — minor build difference
- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — minor
- `/etc/cloud/build.info` — build timestamp differs (expected)

## Next Steps

1. **Remove Podman packages from Dockerfile** — eliminates ~183 diffs. Remove:
   `podman buildah crun conmon catatonit fuse-overlayfs fuse3 slirp4netns passt
netavark aardvark-dns uidmap containernetworking-plugins`. These were included
   by mistake; the live container only has Docker CE, not Podman.

2. **Add `/root/.zshrc` to rootfs** — live has it, built doesn't. Capture it
   and add to `rootfs/root/`.

3. **Address libpng16 hash drift** — same size/version but different binary hash.
   Likely from reproducibility issues in the build. Low priority.

4. **Pin environment-manager** — binary changes between sessions. Already noted
   as volatile; ensure exclusion is in place.

## Docker Migration Notes (2026-02-18)

Migrated from Podman to Docker for building the reconstruction image.

**Key Docker issues in gVisor:**

1. **Proxy requirement**: Docker build containers don't inherit `$https_proxy`
   from the host. Must pass `--build-arg http_proxy/https_proxy` explicitly.
   Docker excludes predefined proxy ARG names from the build cache key, so
   JWT-token proxy URLs don't break layer caching.

2. **gVisor overlay layer limit**: Docker's overlay snapshotter in gVisor is
   limited to ~35 lowerdir entries (empirical limit; NOT a 4096-byte string-length
   issue as the original notes stated — lowerdir uses relative paths like `51/fs`
   not absolute paths). Ubuntu 24.04 base contributes 4 layers; Dockerfile may
   have at most ~31 layer-creating instructions.

   BuildKit groups consecutive ENV/LABEL/CMD instructions into metadata (not
   creating new overlay snapshots) but SHELL, RUN, COPY, WORKDIR each create a
   new snapshot. The critical fix was consolidating all 10 scattered ENV groups
   into a single ENV instruction, saving ~9 layer slots (from ~38 steps to 27).

3. **`docker export | tar -x`**: Used to extract the built image filesystem for
   manifest capture (replaces Podman's `podman mount` direct filesystem access).

## gVisor Build Notes (original)

- **Overlay layer limit**: ~35 lowerdir entries max (empirical gVisor limit, NOT
  a 4096-byte string-length issue). Ubuntu 24.04 base = 4 layers; Dockerfile
  must stay under ~31 new layers. Consolidate COPY/RUN, and put ALL ENV in one
  instruction (saves ~9 slots vs scattered ENVs).
- **Overlay works on tmpfs**: Cache reuse confirmed with multi-layer builds.
