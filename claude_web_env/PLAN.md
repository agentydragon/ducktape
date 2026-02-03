# Current Plan

## Latest Build Results (2026-02-03, build v2)

**Diff Summary**: 2,132 real differences (down from 10,100)

| Category        | Count  |
| --------------- | ------ |
| Identical       | 120.4k |
| Excluded        | 906.6k |
| **Real diffs**  | 2,132  |
| Only in live    | 1,742  |
| Only in built   | 61     |
| Content changed | 329    |

### Remaining Diff Breakdown

**Only in live (1,742):**

| Category   | Count | Main items                                            | Action                 |
| ---------- | ----- | ----------------------------------------------------- | ---------------------- |
| root-local | 1,727 | virtualenv/pnpm caches                                | ✅ Added to exclusions |
| docs       | 9     | age docs, python3 `_static`                           | ✅ Added age package   |
| others     | 6     | stop-hook, .ssh, .bazelrc, .gitconfig, commit signing | ✅ Added to exclusions |

**Only in built (61):**

| Category  | Count | Main items            | Action                                |
| --------- | ----- | --------------------- | ------------------------------------- |
| docs      | 42    | python3.12-doc extras | Minor — live doesn't install this doc |
| etc       | 7     | APT preference files  | ✅ Cleaned up in Dockerfile           |
| usr-share | 12    | python devhelp/info   | Minor — from python3-doc package      |

**Content changed (329):**

| Category        | Count | Cause                                  |
| --------------- | ----- | -------------------------------------- |
| docs            | 28    | Changelog.gz diffs (version mismatch)  |
| other           | 70    | systemd/gnupg binaries (version drift) |
| python-libs     | 52    | Python 3.12 .so files (version drift)  |
| system-binaries | 97    | systemd/gnupg/gdb/login/glib (version) |
| etc             | 2     | deadsnakes sources, pam.d/login        |
| home/root-home  | 3     | .gitconfig, .wget-hsts                 |

## Next Priority: Version Drift (329 files)

The 329 content-changed files are all version drift — the snapshot.ubuntu.com archive (2025-12-01) provides slightly different package versions than what the live container has.

### Live container package versions

```
systemd=255.4-1ubuntu8.12
gnupg=2.4.4-2ubuntu17.4
python3.12=3.12.3-1ubuntu0.10
gdb=15.0.50.20240403-0ubuntu1
login/passwd=1:4.13+dfsg1-4ubuntu3.2
libpam=1.5.3-5ubuntu5.5
util-linux=2.39.3-9ubuntu6.4
binutils=2.42-4ubuntu2.8
```

### Approach

The snapshot archive from 2025-12-01 likely has earlier point releases. Options:

1. **Update snapshot date** to one that has the exact versions
2. **Pin specific versions** with `apt-get install pkg=VERSION`
3. **Use archive.ubuntu.com** with higher priority for these packages

Option 2 is the most reliable. Add version pins to the apt-get install line for drifting packages.

## Completed

- ✅ Build script captures proprietary binaries
- ✅ Documented exclusion minimization goal
- ✅ Removed stripping step (was stripping `/usr/share/doc`, `/usr/include`, etc.)
- ✅ Fixed `/process_api` structure (file, not directory)
- ✅ Renamed `scripts/README` → `README.md`
- ✅ Added `php8.4-sqlite3`, `age` packages
- ✅ Added runtime exclusions (virtualenv, pnpm, .ssh, .bazelrc, projects)
- ✅ Cleaned up APT preference files from built image
- ✅ Diff reduced from 10,100 → 2,132 real differences

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
