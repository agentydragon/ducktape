# Current Plan

## Binary Versions (2026-03-26)

| Binary                | Build ID   | Version/Release                |
| --------------------- | ---------- | ------------------------------ |
| `process_api`         | `91c789ff` | `process_api_2026-03-23-22-49` |
| `environment-manager` | `495ea204` | `release-d84d76b7-ext`         |

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

### RE Status

**process_api (91c789ff)** — 5,704 lines Rust, 10 modules, 1 TODO.
All modules re-verified against binary via string cross-referencing + objdump.
JWT auth, snapstart, container_info.json confirmed present (correcting prior
claims of removal). Only remaining gap: vsock stubs (requires tokio-vsock).

**environment_manager (495ea204)** — 22,743 lines Go, 96 files, 23 TODO(re).
Binary diff proved "nothing changed except obfuscation" was false: Supabase,
Vercel, Antspace, Baku features removed; filestore_url/filesystem_id/jwt added.
Dead code deleted (-1,610 lines), 10 missing files created (+868 lines), new
fields added. Binary diff documented in BINDIFF_RESULTS.md.

### RE Priorities

1. **env_manager: claude_code_executor.go** — 8 TODO(re). Core function that
   launches Claude Code. Missing: 2 closures (pipe cleanup, output writer),
   4 env var source values (USE_CCR_V2, WORKER_EPOCH, etc.)

2. **env_manager: deploy action filestore mechanism** — 4 TODO(re). New
   filestore-based deploy replaced Vercel/Antspace. Logic not yet recovered.

3. **env_manager: API backends** — 5 TODO(re) across ccr_backend.go and
   session_ingress_backend.go. Function bodies partially stubbed.

4. **process_api: vsock support** — 1 TODO. Requires adding tokio-vsock crate.
   Low urgency (live deployment uses TCP, not vsock).

5. **env_manager: remaining 6 TODO(re)** — scattered across manager.go,
   mcp/server.go, envtype/anthropic/config.go, cmd_task_run.go, streamer.go.

### Container Build

- Rebuild with updated Dockerfile and regenerate diff report
- Container build/diff hangs on live manifest capture in gVisor (pagemap read
  blocks). May need to capture manifest outside sandbox or use docker-based capture.

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
