# Current Plan

## Latest Build Results (2026-02-04, build v4)

**Diff Summary**: 69 real differences (down from 2,132)

| Category        | Count  |
| --------------- | ------ |
| Identical       | 120.6k |
| Excluded        | 907.0k |
| **Real diffs**  | 69     |
| Only in live    | 11     |
| Only in built   | 0      |
| Content changed | 58     |

### Remaining Diff Breakdown

**Only in live (11):**

| Category      | Count | Main items                                   | Action                               |
| ------------- | ----- | -------------------------------------------- | ------------------------------------ |
| claude-config | 2     | `stats-cache.json`, `stop-hook-git-check.sh` | ✅ Added to exclusions               |
| docs          | 6     | `python3/_static` (from `python3-dev`)       | ✅ Added `python3-dev` to Dockerfile |
| root-local    | 3     | `pnpm-state.json`                            | ✅ Added to exclusions               |

**Content changed (58):**

| Category        | Count | Cause                                            |
| --------------- | ----- | ------------------------------------------------ |
| python-libs     | 52    | Python 3.12 `.so` files (version drift 0.9→0.10) |
| docs            | 2     | libpng/python3.12 changelog.gz                   |
| system-binaries | 1     | `python3.12` binary                              |
| system-libs     | 3     | `libpng16.a`, `libpng16.so`, `libpython3.12.so`  |

All 58 content-changed files are python3.12 version drift (3.12.3-1ubuntu0.9 vs 0.10) or libpng.
Python3.12 can't be pinned via APT preferences due to deadsnakes PPA priority conflict.

## Next Priority: Python 3.12 Version Drift (58 files)

The remaining 58 content-changed files are all from the python3.12 package family
(snapshot has 3.12.3-1ubuntu0.9, live has 3.12.3-1ubuntu0.10). The version-drift-pin
approach doesn't work for python3.12 because the deadsnakes PPA pin (priority 1002)
creates a conflict.

### Possible approaches

1. **Update snapshot date** to one after python3.12 0.10 was released
2. **Explicit `apt-get install python3.12=VERSION`** in the Python install step
3. **Exclude python3.12 version drift** in exclusions.yaml (accept the drift)

## Completed

- ✅ Build script captures proprietary binaries
- ✅ Documented exclusion minimization goal
- ✅ Removed stripping step (was stripping `/usr/share/doc`, `/usr/include`, etc.)
- ✅ Fixed `/process_api` structure (file, not directory)
- ✅ Renamed `scripts/README` → `README.md`
- ✅ Added `php8.4-sqlite3`, `age`, `python3-dev` packages
- ✅ Added runtime exclusions (virtualenv, pnpm, .ssh, .bazelrc, projects, claude state)
- ✅ Cleaned up APT preference files from built image
- ✅ Diff reduced from 10,100 → 2,132 → 69 real differences
- ✅ Version-drift-pin file pins 18 package families to live versions
- ✅ Fixed `.gitconfig` (added `[core]` section, fixed indentation)
- ✅ Fixed deadsnakes sources (trailing space on `Signed-By:`)
- ✅ Removed `python3-doc` (not in live container)
- ✅ Updated glib pin version (ubuntu3.5 → ubuntu3.7)
- ✅ Debug SHELL shows last 100 lines on build failure

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
