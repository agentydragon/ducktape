# Current Plan

## Build Status

**Diff Summary**: 0 real differences (build v17, 2026-02-11)

| Category       | Count   |
| -------------- | ------- |
| Identical      | 121,052 |
| Excluded       | 501,951 |
| **Real diffs** | **0**   |

### Exclusion utilization

116 patterns, 25 unused. Unused patterns are defensive (volatile tools
installed from HEAD, runtime-only artifacts, or session hook state that
can't be tested from within the container).

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

## gVisor Build Notes

- **Kernel keyring quota**: `crun-gvisor-wrapper` injects `--no-new-keyring`
  to prevent keyring exhaustion (~60-70 limit per session).
- **Overlay layer limit**: ~47-50 layers per overlay stack (kernel 4096-byte
  mount options string limit). Use `--layers=false` for large builds.
- **Overlay works on tmpfs**: Cache reuse confirmed with 90-layer builds.
