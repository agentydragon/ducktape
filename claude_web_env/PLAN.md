# Current Plan

## Latest Build Results (2026-02-05, build v14, corrected capture)

**Diff Summary**: 112 real differences

| Category        | Count     |
| --------------- | --------- |
| Identical       | 120,576   |
| Excluded        | 1,664,740 |
| **Real diffs**  | **112**   |
| Only in built   | 1         |
| Content changed | 111       |

Previous session reported 4,149 diffs — this was a **manifest capture bug**:
the built manifest was captured from a raw VFS layer directory instead of a
properly `podman mount`-ed container, missing ~8,000 entries (all of `/var/`,
parts of `/usr/share/`). After recapture with `podman create` + `podman mount`,
the actual diff is 112.

### Remaining diff breakdown (112 files)

**Python 3.13 from deadsnakes PPA (108 files):**

- 97 `.py` source files, ~30 `.so` modules, 6 headers, 4 changelogs,
  1 binary, 1 only-in-built (`module_docs.py` — new file in newer version)
- Live: `3.13.11-1+noble1`, PPA now serves `3.13.12-1+noble1`
- Root cause: deadsnakes PPA only keeps the latest version; no snapshot/archive
  mechanism exists. The exact version in live has been superseded.

**libpng (2 files):**

- Live: `1.6.43-5ubuntu0.3` (security update), Built: `1.6.43-5build1`
- The `.3` security update was released after the snapshot date (2025-12-01)
  and has since been superseded by `.4`.

**linux-libc-dev (1 file):**

- Live: newer kernel headers, Built: snapshot version
- Same pattern — security update released after snapshot date.

### Exclusion utilization

109 patterns, 10 unused:

- 1 `skip_paths`: `/nix` (defensive — Nix installed by session hooks at runtime)
- 3 `volatile_paths`: `**/__pycache__`, `/var/lib/sgml-base/**`,
  `/var/lib/systemd/**` (currently identical between sides, but genuinely
  volatile across builds — keep as defensive)
- 1 `only_in_live`: `/var/lib/dpkg/alternatives/python3` (currently identical
  on both sides — keep as defensive)
- 5 `session_hook_artifacts`: runtime-only, untestable from within container

### What could reduce the diff further

1. **Pin python3.13 via cached `.deb` files (108→0):** Download the exact
   `3.13.11` `.deb` files from the deadsnakes PPA and cache them in the repo
   or a build cache. Install with `dpkg -i` instead of `apt-get install`.
   This is the only way to pin a PPA that doesn't support snapshots.
   Affects: `python3.13`, `python3.13-dev`, `python3.13-venv`,
   `libpython3.13-stdlib`, `libpython3.13-dev`, `libpython3.13`.

2. **Update snapshot date for libpng and linux-libc-dev (3→0):** Move the
   Ubuntu snapshot from 2025-12-01 to a date after the security updates were
   published. Risk: this may change other package versions and introduce new
   diffs. Alternatively, pin these 2 packages to their exact live versions
   using APT preferences.

3. **Accept python3.13 drift as inherent:** The 108 python3.13 diffs are all
   from a PPA that fundamentally doesn't support version pinning. If caching
   `.deb` files isn't worth the maintenance burden, these can be moved to
   `volatile_paths` as `/usr/lib/python3.13/**` + `/usr/include/python3.13/**`
   - `/usr/bin/python3.13`. This would give 0 actionable diffs but at the
     cost of not tracking python3.13 content changes.

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
- **Per-pattern match counts**: `diff_manifests.py` now tracks and reports how
  many files each exclusion pattern matched, enabling data-driven trimming.
- **Manifest capture fix**: Built manifest must use `podman create` + `podman
mount` (not raw VFS dir access) to get the properly merged container
  filesystem.

## Completed

- ✅ Manifest capture bug found and fixed (VFS raw dir → `podman mount`)
- ✅ Removed `/work` from `skip_paths` (was unused defensive pattern)
- ✅ Per-pattern match count reporting in diff script
- ✅ Exclusion pattern trimming (144 → 109 patterns, 35 unused removed)
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
