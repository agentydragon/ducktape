# Current Plan

## Latest Build Results (2026-02-04, build v13)

**Diff Summary**: ~113 real differences (down from 10,100 → 2,132 → 69 → 0 → 114)

| Category        | Count  |
| --------------- | ------ |
| Identical       | 120.6k |
| Excluded        | 907.1k |
| **Real diffs**  | ~113   |
| Only in built   | 1      |
| Content changed | ~112   |

### Remaining Diff Breakdown

All remaining diffs are **genuinely unpinnable** — the exact versions in the live
container have been superseded and no longer exist in any available repository.

**Python 3.13 (deadsnakes PPA) — ~110 files:**

The deadsnakes PPA only keeps the latest version. Live has `3.13.11-1+noble1`,
PPA now serves `3.13.12-1+noble1`. Files affected: `.py` libs, `.so` modules,
headers, binary, changelogs.

**libpng — 2 files:**

Snapshot (2025-12-01) has `1.6.43-5build1`, live has `1.6.43-5ubuntu0.3`
(security update released after snapshot date, since superseded by `.4`).

**linux-libc-dev — 1 file:**

Snapshot has `6.8.0-88.89`, live has `6.8.0-90.91` (security update released
after snapshot date, since superseded by `6.8.0-94.96`).

### What's been pinned successfully

Version-drift pins now match the live container for **30+ package families**:

- System core: libc6, libssl, systemd, util-linux, gnupg, login, binutils,
  libpam, bsdutils, linux-libc-dev (attempted)
- Libraries: libxml2, libxslt, libsodium, libtasn1, libavahi, libcups, libheif,
  libdrm, mesa, libboost, libldap, libapparmor, fonts-opensymbol
- Runtimes: python3.12 (exact version), openjdk-21, python3-apt

### Build infrastructure improvements

- **HTTPS for snapshot**: Switched from HTTP to HTTPS for `snapshot.ubuntu.com`
  to avoid 503 errors from the TLS-inspecting egress proxy.
- **-dev package pins**: Added `libblkid-dev`, `libmount-dev`, `uuid-dev` to
  the util-linux pin group to resolve exact-version dependency conflicts.

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
