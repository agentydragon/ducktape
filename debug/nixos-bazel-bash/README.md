# NixOS Bazel compatibility issues

## Summary

Bazel doesn't work locally on NixOS. Three independent issues must be fixed.

## Issue 1: `/bin/bash` not found

**Symptom**: Ruff lint aspect fails with `execvp(/bin/bash, ...): No such file or directory`

**Root cause**: `aspect_rules_lint` calls `ctx.actions.run_shell()` without `shell=`. Bazel defaults to `/bin/bash`, which doesn't exist on NixOS.

**Fix**: Add to user `~/.bazelrc`:

```
startup --shell_executable=/run/current-system/sw/bin/bash
```

This is NixOS-specific — belongs in user bazelrc, not project `.bazelrc`.

**Verified**: This fixes the ruff aspect.

## Issue 2: `env` not on PATH in sandbox actions

**Symptom**: Mypy lint aspect fails with `exec: env: not found` at line 312 of the `rules_python` bootstrap script.

**Root cause**: The `rules_python` bootstrap script (`interpreter_tmpl.sh`, generated for all `py_binary` targets) does:

```bash
command=(
  env                          # bare 'env' command, needs PATH
  "${interpreter_env[@]}"      # PYTHONSAFEPATH=1
  "$python_exe"
  ...
)
exec "${command[@]}"
```

The mypy aspect's `ctx.actions.run()` env (from `rules_mypy` fork at `mypy/private/mypy.bzl:227`) merges `ctx.configuration.default_shell_env`, which in Bazel 8.x with `--incompatible_strict_action_env` returns only `{"TMPDIR": "/tmp"}` — no PATH. The sandbox action runs with **no PATH at all**.

The `env` call is redundant — bash can do `export VAR=val; exec "$python_exe"` natively.

**Workaround**: Add to user `~/.bazelrc`:

```
build --action_env=PATH
build --host_action_env=PATH
```

This inherits the host PATH into sandbox actions. Matches what the nixpkgs-patched Bazel does internally.

**Proper fix**: Patch `rules_python` bootstrap to use bash builtins instead of `env`:

```bash
# Current (broken without PATH):
exec env PYTHONSAFEPATH=1 "$python_exe" ...

# Fixed (no external dependency):
export PYTHONSAFEPATH=1
exec "$python_exe" ...
```

This could be upstreamed to `rules_python` or applied via Bazel module override.

## Issue 3: BuildBuddy CC toolchain probes FHS paths

**Symptom**: `no such package '@@toolchains_buildbuddy++buildbuddy+buildbuddy_toolchain//'`: `Inconsistent filesystem operations. Encountered error '/usr/bin/ld.gold (No such file or directory)'`

**Root cause**: The `toolchains_buildbuddy` repo rule (`rules.bzl:85-87`) unconditionally symlinks FHS linker paths during fetch:

```python
rctx.symlink("/usr/bin/ar", "bin/ar")
rctx.symlink("/usr/bin/ld", "bin/ld")
rctx.symlink("/usr/bin/ld.gold", "bin/ld.gold")
```

These don't exist on NixOS. The symlinks are only needed for local CC actions — RBE workers have their own linkers. But the repo rule fails during fetch before any action runs.

**Fix options**:

1. **Patch `toolchains_buildbuddy` via module override**: Make the symlinks conditional on the paths existing (`rctx.which("ld.gold")` or `rctx.path("/usr/bin/ld.gold").exists`)
2. **Provide the expected paths**: Install `binutils` in a way that populates `/usr/bin/ld.gold` (e.g., via `environment.pathsToLink` or a NixOS module)
3. **Fork `toolchains_buildbuddy`**: Add a `skip_local_cc_symlinks` attribute to the repo rule

Option 1 is simplest. The patch would change the repo rule to:

```python
if not rctx.os.name.lower().startswith("windows"):
    for tool in ["ar", "ld", "ld.gold"]:
        path = "/usr/bin/" + tool
        result = rctx.execute(["test", "-e", path])
        if result.return_code == 0:
            rctx.symlink(path, "bin/" + tool)
```

## Investigated and rejected: envfs

`services.envfs.enable = true` mounts a FUSE filesystem at `/bin` and `/usr/bin` that resolves executables from the calling process's PATH at `execve` time. We tested this — it makes `/bin/bash` executable but can't fix the empty PATH inside Bazel sandbox actions. `--shell_executable` + `--action_env=PATH` solve both problems without envfs.

## Investigated and rejected: nixpkgs-patched Bazel

nixpkgs patches Bazel's Java source code at build time, replacing `/bin/bash` with a nix-store bash and injecting nix-store coreutils into the strict action env PATH. This fixes issues 1 and 2 but not issue 3 (BuildBuddy toolchain). It also loses `.bazelversion` pinning and builds Bazel from source (~16GB vendored deps). Not worth it given that `.bazelrc` workarounds + a toolchain patch cover all three issues.

## Combined workaround (`~/.bazelrc`)

```
startup --shell_executable=/run/current-system/sw/bin/bash
build --action_env=PATH
build --host_action_env=PATH
```

This fixes issues 1 and 2. Issue 3 requires a separate toolchain patch.

## Status

| Issue              | Symptom                           | Fix                                     | Status      |
| ------------------ | --------------------------------- | --------------------------------------- | ----------- |
| `/bin/bash`        | Ruff lint fails                   | nixpkgs `bazel_8` already patched       | Resolved    |
| Empty PATH         | Mypy lint fails (`env` not found) | `--action_env=PATH` in `~/.bazelrc`     | Applied     |
| `/usr/bin/ld.gold` | BuildBuddy toolchain fetch fails  | Patch `toolchains_buildbuddy` repo rule | Not started |

Docker container test setup for reproducing these issues is at `devinfra/nixos_bazel_test/`.

## Key files

| File                                                      | Role                                      |
| --------------------------------------------------------- | ----------------------------------------- |
| `~/.bazelrc`                                              | User-local NixOS workarounds (issues 1-2) |
| `MODULE.bazel:336-349`                                    | BuildBuddy toolchain registration         |
| External `toolchains_buildbuddy+/rules.bzl:84-87`         | FHS symlinks that break on NixOS          |
| External `rules_mypy~/mypy/private/mypy.bzl:227`          | Action env missing PATH                   |
| External `rules_python` bootstrap (`interpreter_tmpl.sh`) | Uses bare `env` command                   |
