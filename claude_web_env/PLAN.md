# Current Plan

## Latest Build Results (2026-02-05, build v14)

**Diff Summary**: 4,149 real differences (up from ~113 in v13 due to new
only-in-live files, not regressions in content matching)

| Category        | Count     |
| --------------- | --------- |
| Identical       | 115,397   |
| Excluded        | 1,669,595 |
| **Real diffs**  | **4,149** |
| Only in live    | 4,036     |
| Only in built   | 2         |
| Content changed | 111       |

### Why the diff count increased from v13

The live manifest now captures 1.76M entries (vs ~1M previously) because the
container has accumulated more runtime files. The increase is almost entirely
**only-in-live** files, not content regressions:

- **3,802 PHP files** in `/usr/share/php8.4-*` and `/var/lib/php/modules/8.4/`
  — present in live but missing from built image. Likely a missing PHP package
  or module registration step in the Dockerfile. Needs investigation.
- **228 `/var` entries** — directories and state files created at runtime
  (`/var/lib/pam`, `/var/lib/php`, `/var/cache/PackageKit`, etc.).
- **6 python3.12 source files** in `/usr/src/python3.12/` — only in live.

### Content-changed breakdown (111 files)

All content-changed diffs remain **genuinely unpinnable** — the exact versions
in the live container have been superseded.

**Python 3.13 (deadsnakes PPA) — 97 `.py`/`.so` files + 6 headers + 4 docs + 1 binary:**

The deadsnakes PPA only keeps the latest version. Live has `3.13.11-1+noble1`,
PPA now serves `3.13.12-1+noble1`.

**libpng — 2 files:**

Snapshot (2025-12-01) has `1.6.43-5build1`, live has `1.6.43-5ubuntu0.3`
(security update since superseded).

**linux-libc-dev — 1 file:**

Snapshot has older version, live has newer security update (since superseded).

### Exclusion pattern cleanup

Reduced exclusion patterns from **144 → 110** by removing 34 patterns with 0
hits. The diff script now reports per-pattern match counts.

| Category                 | Before | After | Removed |
| ------------------------ | ------ | ----- | ------- |
| `skip_paths`             | 39     | 31    | 8       |
| `volatile_paths`         | 53     | 44    | 9       |
| `only_in_live`           | 33     | 24    | 9       |
| `only_in_built`          | 14     | 6     | 8       |
| `session_hook_artifacts` | 5      | 5     | 0       |
| **Total**                | 144    | 110   | 34      |

After trimming, only 7 patterns remain unused:

- 2 defensive `skip_paths` (`/nix`, `/work`) — not populated in this capture
  but legitimately expected in other containers
- 5 `session_hook_artifacts` — created by session start hooks at runtime,
  cannot be tested from within the container

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
- **SHELL error visibility**: Build SHELL directive now dumps last 200 lines
  of build log on failure (saves fd 3 as original stderr, traps ERR).
- **Per-pattern match counts**: `diff-manifests.py` now tracks and reports how
  many files each exclusion pattern matched, enabling data-driven trimming.

### Next steps

1. **Fix PHP files only-in-live**: Investigate why 3,802 `/usr/share/php8.4-*`
   files are present in live but missing from built image. Likely need to add
   `php8.4-common` or related packages, or trigger module registration.
2. **Fix `/var` only-in-live**: Add missing `/var` paths to `skip_paths` or
   `only_in_live` (runtime state dirs like `/var/lib/pam`, `/var/cache/PackageKit`).
3. **Fix python3.12 source**: Add `python3.12-dev` or equivalent package to get
   `/usr/src/python3.12/` grammar files.
4. **Pin python3.13**: Requires snapshot of deadsnakes PPA or freezing the version.

## Completed

- ✅ Per-pattern match count reporting in diff script
- ✅ Exclusion pattern trimming (144 → 110 patterns, 34 unused removed)
- ✅ SHELL directive error visibility (dumps last 200 lines on build failure)
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
