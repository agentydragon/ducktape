# Current Plan

## Build Status

**Diff Summary**: 4 real differences (build v20, 2026-03-16)

| Category       | Count   |
| -------------- | ------- |
| Identical      | 120,695 |
| Excluded       | 562,198 |
| **Real diffs** | **4**   |

Note: these counts are from the v19 build. A rebuild with the updated Dockerfile
(2026-03-16 snapshot) may change the counts.

## Real Difference Analysis

All remaining diffs are minor build non-determinism — no gaps in the Dockerfile.

### Content changed (4)

- `/usr/lib/x86_64-linux-gnu/libpng16.so.16.43.0` — same size, hash differs
- `/usr/lib/x86_64-linux-gnu/libpng16.a` — size 357068->355524
- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — size 2108->1501
- `/var/lib/dpkg/status` — size 687887->687872 (15-byte difference; likely a newline
  or minor apt metadata variation)

**libpng16**: Three files with the same package version but different binary content.
Known apt snapshot reproducibility issue — functionally identical.

**dpkg/status**: 15-byte difference. Likely trailing whitespace or minor metadata.

## Next Steps

- Rebuild container with updated Dockerfile and regenerate diff report
- Container diff may reveal new package versions from the 2026-03-16 snapshot

### Optional future work

1. **Add libpng16 to `hash_may_differ`** — eliminates the remaining 3 libpng diffs.
2. **Investigate dpkg/status 15-byte diff** — identify the exact difference.

## Docker Build Notes

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
