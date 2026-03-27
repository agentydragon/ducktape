# Session Start Hook Race & 7s Startup — RCA (2026-03)

## Incident

Session `ff515ef9` failed to run SessionStart because the daemon took 7.2 seconds
to start, exceeding the 5s timeout in the installed wheel (`claude-hooks-6009197`).

## Root Cause Chain

### 1. TOCTOU race (missing FileLock in installed wheel)

At 02:23:06.7, five hook processes spawned simultaneously:

- 4 `InstructionsLoaded` hooks for session `46cb7ed2`
- 1 `SessionStart` for `ff515ef9`

All 4 InstructionsLoaded hooks found no daemon socket → each called `_start_daemon()`
(no lock). TOCTOU race on pidfile → all 4 spawned separate daemons for `46cb7ed2`.
5 concurrent Python processes all importing from `/nix/store` on 9p/gVisor.

The fix (FileLock in `_ensure_daemon()`) was added in commit `89a9043` in repo HEAD
but was **not in the installed wheel** due to the cascade below.

### 2. Stale installed wheel (claude-hooks-6009197)

| Location                    | `client.py`                     | Timeout | Has `FileLock` |
| --------------------------- | ------------------------------- | ------- | -------------- |
| Installed wheel (`6009197`) | `_start_daemon()` no lock       | 5s      | ❌             |
| Repo HEAD                   | `_ensure_daemon()` + `FileLock` | 15s     | ✅             |

**Why CI never released a correct wheel:**
After `89a9043` added FileLock, a cascade of CI failures (mypy, RE compilation) prevented
`bazel build //:claude_hooks_wheel` from succeeding. No `claude-hooks-*` releases exist
between `5f12ef1` and `6009197`. All bump commits during that period bumped skills/ducktape
only.

**Why the manually created wheel had stale content:**
When `bazel build //:claude_hooks_wheel` was run at `6009197`, RBE returned a cached action
result from before the FileLock changes. The resulting wheel was uploaded with stale content.

**Why `--ref "$FULL_SHA"` made it worse:**
`gh workflow run release.yml --ref "$FULL_SHA"` (bare SHA) is rejected by GitHub API, so
`release.yml` never ran to update `npins/sources.json`. Fixed in `eec3614` (`--ref devel
--field sha=...`).

## Daemon Startup Time Profile

Measured on native Linux (ext4), repo HEAD, Nix wheel Python:

| Phase                                   | Time      |
| --------------------------------------- | --------- |
| `fastapi` import                        | ~0.35s    |
| `kubernetes` import (via `k8s_secrets`) | ~0.38s    |
| `session_start.handler` total           | ~0.61s    |
| Other imports + uvicorn bind            | ~0.15s    |
| **Total import + socket ready**         | **~1.1s** |

Runs: 1085ms, 1086ms, 1033ms.

On gVisor/9p, each Nix store read is ~3–5× slower → ~3–5s per process.
With 5 concurrent processes: I/O contention amplifies the worst case to 7.2s.

**Heaviest imports:**

- `fastapi`: 0.35s
- `kubernetes.client`: 0.38s (only needed for SessionStart k8s secrets fetch)

## Fixes Applied

1. `89a9043`: FileLock in `_ensure_daemon()` (repo, needs new release)
2. `6579b25`, `814b940`: mypy/lint fixes unblocking CI
3. `eec3614`: Fix `--ref "$FULL_SHA"` bug in `bb_release.sh`
4. `47ceef7`: Include changed packages in bump commit titles

## Potential Future Improvements

- Lazy-import `kubernetes` (only needed in SessionStart handler, not for every hook call)
- Consider increasing default timeout further (15s is already an improvement over 5s)
- Monitor release pipeline to ensure claude-hooks wheel stays current
