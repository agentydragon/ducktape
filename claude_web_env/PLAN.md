# Current Plan

## Latest Build Results (2026-02-03)

**Diff Summary**: 10,100 real differences

| Category        | Count |
| --------------- | ----- |
| Identical       | 112k  |
| Excluded        | 900k  |
| **Real diffs**  | 10.1k |
| Only in live    | 9,780 |
| Only in built   | 19    |
| Content changed | 300   |
| Type changed    | 1     |

**Root cause of most differences**: Dockerfile strips `/usr/share/doc`, `/usr/share/man`, `/usr/include` — but the live container has these files.

## Priority 1: Remove Stripping Step

Per the exclusion minimization goal, we should match the live container, not add exclusions for stripped files.

**Files currently stripped (Dockerfile line ~333)**:

- `/usr/share/doc` (2145 files missing)
- `/usr/share/doc-base`
- `/usr/share/man`
- `/usr/share/info`
- `/usr/include` (4631 files missing)
- `/usr/sbin`

**Action**: Remove the stripping step from Dockerfile.

## Priority 2: Add Runtime-Only Exclusions

These are legitimate runtime artifacts, not reproducibility failures:

| Path                        | Reason               |
| --------------------------- | -------------------- |
| `/home/claude/.npm`         | npm cache at runtime |
| `/home/claude/.cache`       | runtime cache        |
| `/root/.claude/projects`    | session files        |
| `/root/.claude/stop-hook-*` | stop hook scripts    |

## Priority 3: Fix Structure Mismatches

| Issue                          | Live    | Built     | Fix                       |
| ------------------------------ | ------- | --------- | ------------------------- |
| `/process_api`                 | file    | directory | Restructure in Dockerfile |
| `/usr/local/bin/httpx`         | absent  | present   | Remove from Dockerfile    |
| `/usr/local/bin/websockets`    | absent  | present   | Remove from Dockerfile    |
| `/home/claude/scripts/README`  | absent  | present   | Remove from Dockerfile    |
| `/usr/lib/jvm/.../docs`        | absent  | present   | Don't install java docs   |
| `/etc/php/8.4/.../sqlite3.ini` | present | absent    | Install php-sqlite3       |

## Priority 4: Version Drift (300 files)

Same-size binaries with different hashes. Mostly systemd and gnupg components.

Packages needing pinning:

- `systemd` and related (`systemd-sysv`, `libsystemd0`, etc.)
- `gnupg` and related
- Anything else showing hash-only drift

Method:

1. Get exact versions from live: `dpkg-query -W -f='${Package}=${Version}\n' | grep systemd`
2. Pin in Dockerfile: `apt-get install systemd=VERSION`

## Completed

- ✅ Build script captures proprietary binaries
- ✅ Documented exclusion minimization goal in AGENTS.md and README.md
- ✅ Full build completed (60 steps, image size 5.66GB)
- ✅ Diff report generated (10,100 real differences)

## Session Notes

### gVisor Limitations and Fixes

1. **Kernel keyring quota**: ✅ **FIXED** — `crun-gvisor-wrapper` now injects
   `--no-new-keyring` to prevent keyring creation. Tested with 70+ RUN steps.
2. **Overlay layer limit**: ~47-50 layers per overlay stack.
   - Kernel limit: 4096 bytes (1 page) for mount options string
   - Per-layer: ~80 bytes (graphroot path + 26-char symlink + separator)
   - Empirically verified: 90 layers at 4066 bytes succeeded, 91 at 4110 bytes failed
   - containers/storage deduplicates layers across images, so effective limit varies
3. **Overlay works on tmpfs**: Tested with 90-layer build — cache reuse confirmed.

### Keyring Fix Details

- **Root cause**: crun creates a session keyring for each container via
  `keyctl(KEYCTL_JOIN_SESSION_KEYRING)` for credential isolation.
- **gVisor limit**: ~60-70 keyrings per session, not recoverable.
- **Solution**: `--no-new-keyring` flag tells crun to skip keyring creation.
- **Implementation**: `tools/claude_hooks/config/podman/crun_gvisor_wrapper.py`
  now injects this flag for all `crun create` and `crun run` commands.
