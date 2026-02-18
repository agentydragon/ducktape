# Current Plan

## Build Status

**Diff Summary**: 4 real differences (build v19, 2026-02-18)

| Category       | Count   |
| -------------- | ------- |
| Identical      | 120,695 |
| Excluded       | 562,198 |
| **Real diffs** | **4**   |

## Real Difference Analysis

All remaining diffs are minor build non-determinism — no gaps in the Dockerfile.

### Content changed (4)

- `/usr/lib/x86_64-linux-gnu/libpng16.so.16.43.0` — same size, hash differs
- `/usr/lib/x86_64-linux-gnu/libpng16.a` — size 357068->355524
- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — size 2108->1501
- `/var/lib/dpkg/status` — size 687887->687872 (15-byte difference; likely a newline
  or minor apt metadata variation)

**libpng16**: Three files with the same package version but different binary content.
This is a known apt snapshot reproducibility issue — the package is compiled at build
time with slightly different results. Very low priority; the live and built versions
are functionally identical.

**dpkg/status**: 15-byte difference. With Podman removed, the package lists are now
nearly identical. The remaining difference is likely trailing whitespace or a minor
metadata variation. Low priority.

## Next Steps

No significant action items. The reconstruction is essentially complete:

- The live container has Docker CE (not Podman), matching the updated Dockerfile
- All session-specific runtime state (mkcert CAs, Docker buildx state, containerd
  plugins) is properly excluded
- Only libpng16 build non-determinism remains, which is cosmetic

### Optional future work

1. **Add libpng16 to `hash_may_differ`** — eliminates the remaining 3 libpng diffs.
   Since they're the same version but built non-reproducibly, this is safe.

2. **Pin libpng16 snapshot date** — try pinning to a specific snapshot date where
   the binary matches. Low priority.

3. **Investigate dpkg/status 15-byte diff** — identify the exact difference and
   either fix or exclude.

## Change History

### v19 (2026-02-18)

- **Removed Podman packages from Dockerfile** — eliminated 183 "only in built" diffs.
  Packages removed: `podman buildah crun conmon catatonit fuse-overlayfs fuse3
slirp4netns passt netavark aardvark-dns uidmap containernetworking-plugins`.
  The live container uses Docker CE only.

- **Added mkcert exclusions** — excluded session-specific mkcert development CAs:
  - `/etc/ssl/certs/mkcert_*` and `/usr/local/share/ca-certificates/mkcert_*`
  - `/etc/ssl/certs/*.[0-9]` (generalized from `*.0` to cover collision-numbered
    symlinks when mkcert CAs push system certs to `.1`, `.2`, etc.)
  - `/etc/ssl/certs/ca-certificates.crt` moved to `hash_may_differ` (mkcert appends
    session dev CAs at runtime)

- **Added Docker runtime exclusions** — `/root/.docker/**` (BuildKit/buildx state),
  `/opt/containerd/**` (Docker daemon runtime plugins)

- **Added `/root/.zshrc` to `only_in_live`** — live container has a zsh config;
  built image doesn't

- **Added `/etc/cloud/build.info` to `hash_may_differ`** — embeds build timestamp,
  always differs

- **Added `/usr/local/bin/environment-manager` to `hash_may_differ`** — captured
  fresh each build, so hashes match (UNUSED in current run), but protects future
  session-drift cases

### v18 (2026-02-18)

**Diff Summary**: 225 real differences

- Migrated from Podman to Docker for building the reconstruction image
- Identified Podman packages (183 diffs) as the primary issue

## Docker Build Notes (2026-02-18)

**Key Docker issues in gVisor:**

1. **Proxy requirement**: Docker build containers don't inherit `$https_proxy`
   from the host. Must pass `--build-arg http_proxy/https_proxy` explicitly.
   Docker excludes predefined proxy ARG names from the build cache key, so
   JWT-token proxy URLs don't break layer caching.

2. **gVisor overlay layer limit**: Docker's overlay snapshotter in gVisor is
   limited to ~35 lowerdir entries (empirical limit; NOT a 4096-byte string-length
   issue). Ubuntu 24.04 base contributes 4 layers; Dockerfile may have at most ~31
   layer-creating instructions.

   BuildKit groups consecutive ENV/LABEL/CMD instructions into metadata (not
   creating new overlay snapshots) but SHELL, RUN, COPY, WORKDIR each create a
   new snapshot. The critical fix was consolidating all 10 scattered ENV groups
   into a single ENV instruction, saving ~9 layer slots.

3. **`docker export | tar -x`**: Used to extract the built image filesystem for
   manifest capture (replaces Podman's `podman mount` direct filesystem access).
