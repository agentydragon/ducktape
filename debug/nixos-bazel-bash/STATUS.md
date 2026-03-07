# NixOS + Bazel + RBE: Current Status

## Verified Working

Bazel builds and tests pass on a real NixOS system with RBE enabled. Tested packages:

| Package                       | Build | Test          |
| ----------------------------- | ----- | ------------- |
| `//agent_core/...`            | pass  | 8/8 pass      |
| `//cluster/kubespan_agent/..` | pass  | (Go, no test) |
| `//mcp_infra/...`             | pass  | 40/40 pass    |
| `//openai_utils/...`          | pass  | 4/4 pass      |
| `//props/backend/...`         | pass  | 9/9 pass      |
| `//props/frontend/...`        | pass  | n/a           |
| `//util/...`                  | pass  | 2/2 pass      |
| `//tana/...`                  | pass  | 2/2 pass      |

## Required `~/.bazelrc` for NixOS + RBE

```
build --shell_executable=/bin/bash
build --host_action_env=PATH=/run/current-system/sw/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
build --host_action_env=NIX_LD
build --host_action_env=NIX_LD_LIBRARY_PATH
common --repo_env=NIX_LD
common --repo_env=NIX_LD_LIBRARY_PATH
```

Managed by `nix/home/modules/nixos-bazel.nix` via home-manager.

### Env var scoping

- **`--host_action_env`**: exec-config (host) tools running locally. Not `--action_env`,
  which would leak NixOS paths (`NIX_LD` nix-store path, `/run/current-system/sw/bin`)
  to RBE workers where they don't exist.
- **`--repo_env`**: repository rules (fetching toolchains, Go modules) running locally.
- **No `--action_env`**: target-config actions use Bazel's hermetic toolchains (Python,
  Rust, Node resolve tools via runfiles). Genrules use basic FHS utilities (`tar`, `cp`)
  or `$(location)` references. RBE workers get their own PATH from their container.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  NixOS host (host platform, auto-detected)      │
│                                                 │
│  nixpkgs bazel_8 (patched: /bin/bash → nix)     │
│  programs.nix-ld (dynamic linker stub)          │
│  environment.systemPackages (gcc, python, etc)  │
│  ~/.bazelrc (shell_executable, host_action_env) │
│                                                 │
│  /run/current-system/sw/bin → nix store tools   │
│  /lib64/ld-linux-x86-64.so.2 → nix-ld          │
└─────────────┬───────────────────────────────────┘
              │ RBE actions (spawn_strategy=remote,local)
              ▼
┌─────────────────────────────────────────────────┐
│  BuildBuddy RBE workers (execution platform)    │
│  Ubuntu 24.04 + Firecracker isolation           │
│  /bin/bash ✓  /usr/bin/ar ✓  /usr/bin/ld ✓      │
│  PATH from container (no NixOS paths leaked)    │
│  BuildBuddy GCC toolchain (exec_compatible_with │
│  constraints match //:rbe_linux_x64 only)       │
└─────────────────────────────────────────────────┘
```

## Issues and Fixes

### Issue 1: nixpkgs bazel_8 shell path + RBE (RESOLVED)

**Problem**: nixpkgs patches `bazel_8` to replace `/bin/bash` with a nix-store path. This works locally but breaks RBE — the nix-store path doesn't exist on workers.

**Fix**: `build --shell_executable=/bin/bash` in user `~/.bazelrc`. On NixOS `/bin/bash` exists via the system profile symlink. On RBE workers it exists natively.

### Issue 2: Empty PATH in sandbox actions (RESOLVED)

**Problem**: `--incompatible_strict_action_env` (default in Bazel 8) gives sandbox actions only `TMPDIR=/tmp` — no PATH.

**Fix**: Set PATH via `--host_action_env` for exec-config tools. Target-config actions use hermetic toolchains that resolve tools via runfiles, so they don't need a custom PATH. This avoids leaking NixOS paths to RBE workers.

### Issue 3: `toolchains_buildbuddy` FHS paths (NOT YET HIT)

**Problem**: The `toolchains_buildbuddy` repo rule hardcodes symlinks to `/usr/bin/ar`, `/usr/bin/ld`, `/usr/bin/ld.gold` which don't exist on NixOS.

**Current state**: Has not triggered in testing because the remote cache satisfies the actions. Would fail on a clean build without cache.

**Fix options**:

1. Patch via `single_version_override`: Make symlinks conditional
2. Provide FHS symlinks in NixOS config
3. Fork `toolchains_buildbuddy` with a `skip_local_cc_symlinks` attribute

### Issue 4: nix-ld for Bazel-downloaded binaries (RESOLVED)

**Problem**: Bazel downloads dynamically-linked binaries (python-build-standalone, rustc, node) that expect FHS library paths. On NixOS, these fail with SIGABRT.

**Fix**: `programs.nix-ld.enable = true` + propagate `NIX_LD` and `NIX_LD_LIBRARY_PATH` via `--host_action_env` and `--repo_env` only (not `--action_env`, which would send nix-store paths to RBE workers).

### Issue 5: `--host_platform` conflation (RESOLVED)

**Problem**: `.bazelrc` had `build:rbe --host_platform=//:rbe_linux_x64`, telling Bazel the host machine is the RBE platform. This was a workaround for `BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN=1` (which blocked local CC detection), but that setting was removed.

**Impact**: Bazel couldn't distinguish local vs remote actions for env var scoping. `--action_env` and `--host_action_env` all targeted the same "platform".

**Fix**: Removed `--host_platform=//:rbe_linux_x64`. Host platform is now auto-detected. `//:rbe_linux_x64` is registered only as an execution platform via `--extra_execution_platforms`.

## Why `--host_action_env`, not `--action_env`

Neither flag can be scoped per-platform. `--action_env` applies to all actions (local and remote), so NixOS paths would leak to RBE workers. `--host_action_env` only affects exec-config (host) tools, which always run locally.

## Files

| File                               | Purpose                                   |
| ---------------------------------- | ----------------------------------------- |
| `nix/home/modules/nixos-bazel.nix` | Home-manager module for user `~/.bazelrc` |
| `nix/nixos/modules/bazel-dev.nix`  | NixOS module: nix-ld + dev packages       |
| `debug/nixos-bazel-bash/README.md` | Original investigation notes              |

## Remaining Work

1. **Issue 3**: Will need fixing when remote cache is cold or for clean builds.
2. **Rust**: `finance/worthy` needs `CARGO_BAZEL_REPIN=true` — not NixOS-specific.
