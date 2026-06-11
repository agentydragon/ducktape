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

## Required configuration for NixOS + RBE

### Workspace `.bazelrc` (applies to all users)

```
build --incompatible_strict_action_env
```

Actions get a fixed PATH (`/bin:/usr/bin:/usr/local/bin`) instead of inheriting the
client's PATH. Default in Bazel 9 but not Bazel 8. Without this, the full NixOS PATH
(dozens of `/nix/store/...` paths) leaks to RBE workers where they don't exist.

### NixOS-specific `~/.bazelrc` (via home-manager)

```
build --shell_executable=/bin/bash
build --host_action_env=PATH=/run/current-system/sw/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
test --test_env=PATH=/run/current-system/sw/bin:/bin:/usr/bin:/usr/local/bin
build --host_action_env=NIX_LD
build --host_action_env=NIX_LD_LIBRARY_PATH
common --repo_env=NIX_LD
common --repo_env=NIX_LD_LIBRARY_PATH
```

Managed by `nix/nixos/modules/bazel-nixos/` — installed to `/etc/bazel.bazelrc` via NixOS system config.

### Env var scoping

- **`--incompatible_strict_action_env`**: Gives all actions a fixed PATH
  (`/bin:/usr/bin:/usr/local/bin`). Prevents NixOS path leaking to RBE.
- **`--host_action_env`**: exec-config (host) tools running locally. These need
  `/run/current-system/sw/bin` for NixOS tools.
- **`--test_env=PATH`**: Tests (including `no-remote-exec` tests) need
  `/run/current-system/sw/bin` prepended so `#!/usr/bin/env bash` in test scripts
  finds bash on NixOS (where `/bin/bash` doesn't exist). Harmless on RBE — the
  extra path just doesn't exist.
- **`--repo_env`**: repository rules (fetching toolchains, Go modules) running locally.
- **No `--action_env`**: Would leak to RBE workers.

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

### Issue 2: Client PATH leaking to RBE (RESOLVED)

**Problem**: `--incompatible_strict_action_env` is NOT the default in Bazel 8 (only in
Bazel 9). Without it, Bazel inherits the full client PATH into all actions, including
test actions sent to RBE workers. On NixOS, the client PATH contains dozens of
`/nix/store/...` paths that don't exist on Ubuntu RBE workers, causing
`#!/usr/bin/env bash` in test scripts to fail with "bash: No such file or directory".

**Fix**: Added `build --incompatible_strict_action_env` to workspace `.bazelrc`. This
gives actions a fixed PATH (`/bin:/usr/bin:/usr/local/bin`). For local-only tests on
NixOS, `--test_env=PATH` prepends `/run/current-system/sw/bin` so bash is found.

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

| File                                  | Purpose                               |
| ------------------------------------- | ------------------------------------- |
| `nix/nixos/modules/bazel-nixos/`      | System-level `/etc/bazel.bazelrc`     |
| `nix/nixos/modules/bazel-dev.nix`     | NixOS module: nix-ld + dev packages   |
| `flake.nix` (bazel-test config)       | NixOS container config in flake       |
| `devinfra/nixos_bazel_test/image.nix` | Docker image from NixOS system config |
| `devinfra/nixos_bazel_test/run.sh`    | Build + run script                    |
| `debug/nixos_bazel_bash/README.md`    | Original investigation notes          |

## Known Gap: Genrules with non-trivial shell commands

With strict action env, genrules get PATH=`/bin:/usr/bin:/usr/local/bin`. On NixOS,
some genrules need tools beyond `/bin` basics (e.g., `find`, `sed`, `gzip`). These
tools live in `/run/current-system/sw/bin` on NixOS.

| Target                                  | Missing commands                  |
| --------------------------------------- | --------------------------------- |
| `//cluster/kubespand/qemu:modules_tree` | `mktemp`, `find`, `gunzip`, `sed` |
| `//cluster/kubespand/qemu:initramfs`    | `mktemp`, `find`, `cpio`, `gzip`  |
| `//x/gatelet/server:layers_manifests`   | `awk`                             |

**Options**:

1. Rewrite genrules to use `$(location)` references to Bazel-managed tool targets
2. Tag genrules `no-remote` + use `--action_env=PATH` scoped to local-only actions

## Remaining Work

1. **Issue 3**: Will need fixing when remote cache is cold or for clean builds.
2. **Rust**: `finance/worthy` needs `CARGO_BAZEL_REPIN=true` — not NixOS-specific.
3. **Full `//...` build**: OOM in container when analyzing 2000+ targets. Works in smaller chunks.
4. **Genrule PATH gap**: See "Known Gap" section above.
