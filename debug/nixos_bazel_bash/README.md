# NixOS Bazel compatibility issues

## Summary

Bazel doesn't work locally on NixOS. Three independent issues must be fixed — and a
fourth, [Issue 4](#issue-4-nix-ld-is-environment-based-and-bazel-rulesets-scrub-the-environment),
which is about environment-scrubbed actions and is fixed with filesystem defaults rather
than more env plumbing.

**All of this is about executing actions locally.** With remote execution the question
never arises: actions run on FHS workers, and only repo rules and the odd host tool stay on
the Nix box. That is why ducktape's NixOS hosts feel none of this — `bbr` sends the work to
BuildBuddy. Local execution on Nix glibc is the untested path, and Issue 4 is what lives
there.

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

## Two substrates: NixOS host vs Nix-built container

Issues 1-3 and the module beside this file are about a **NixOS host**. Issue 4 was measured
in a **Nix-built container** (`dockerTools`, no NixOS). They share a glibc and nix-ld, and
nothing else — do not read a conclusion from one as applying to the other.

|                              | NixOS host                                                                           | Nix-built container (`dockerTools`)                                              |
| ---------------------------- | ------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| `NIX_LD`                     | set in the system environment by `programs.nix-ld.enable`; every process inherits it | nothing sets it — must be baked into the image `Env`                             |
| envfs (`/usr/bin`, `/bin`)   | systemd FUSE mount                                                                   | unavailable: needs systemd activation, FUSE, privileges. Static symlinks instead |
| `/run/current-system/sw/bin` | exists                                                                               | **does not exist**                                                               |
| `/etc/bazel.bazelrc`         | installed by this module                                                             | must be baked by the image itself                                                |
| Where actions run            | RBE in practice (`bbr`)                                                              | locally, by construction                                                         |

The third row is the trap: `system.bazelrc` hardcodes `/run/current-system/sw/bin` in both
`--shell_executable` and its PATH lines, so **it cannot be copied into an image verbatim** —
an image needs its own file with store paths or `/bin`. The last row is why the two feel so
different in practice: a NixOS host sends the work to FHS workers and never exercises this,
while a container runs every action on Nix glibc.

## Issue 4: nix-ld and bash both need FILESYSTEM defaults, not environment variables

**Substrate**: measured in a Nix-built container. The NixOS-host half was measured on wyrm2.

**Symptom**: `[nix-ld] FATAL: panicked ... Posix(2)` (ENOENT), or `<cmd>: command not found`
(Exit 127), from an action/test/genrule — never from anything invoked directly.

**Root cause**: Bazel renders actions as `exec env - …`, i.e. with a deliberately emptied
environment. Two Nix-specific defaults are therefore never seen:

|                     | Where it comes from on NixOS                                                  | What a bare container gets                                  |
| ------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------- |
| the ELF interpreter | `/run/current-system/sw/share/nix-ld/lib/ld.so`, nix-ld's compiled-in default | nothing — nix-ld aborts                                     |
| `PATH`              | an FHS `/bin:/usr/bin`                                                        | nixpkgs bash's compiled fallback, literally `/no-such-path` |

**The decisive experiment.** Compile `int main(void){return 0;}` with
`-Wl,--dynamic-linker=/lib64/ld-linux-x86-64.so.2` and run it under `env -`:

|             | NixOS host (wyrm2) | Nix container, before | after |
| ----------- | ------------------ | --------------------- | ----- |
| `NIX_LD`    | **unset**          | set                   | set   |
| `env - ./t` | rc=0               | rc=134 (SIGABRT)      | rc=0  |

Byte-identical nix-ld (store hash `3jbxih2a7…`) throughout — so it was never the binary and
never the environment. A NixOS host works _because of a directory_, not because of `NIX_LD`:
`programs.nix-ld.enable` sets those vars only in `environment.sessionVariables`, which reach
login shells and not systemd services. The env vars are a convenience; the filesystem is the
mechanism.

**The rule**: when porting NixOS behaviour anywhere actions run with a scrubbed environment,
**port the filesystem defaults, not the env vars.** Concretely, build nixpkgs'
`nix-ld-libraries` buildEnv and place it where nix-ld's compiled-in default points, and give
Bazel a `--shell_executable` wrapper that restores `/bin` on PATH when it is missing.

Chasing the same failures with `--action_env`/`--test_env` passthroughs is whack-a-mole: each
ruleset scrubs differently (`rules_python`'s `env -`, `rules_js`'s `js_binary` wrapper,
`tar.bzl`'s mtree rule), the list is open-ended, and rules that set
`use_default_shell_env = False` cannot be reached by those flags at all. Filesystem defaults
fix every one of them at once.

**Also true, and unrelated to environments**: use nixpkgs' Bazel rather than a bazelisk
download. Bazel's extracted helpers (`process-wrapper`, `linux-sandbox`) are FHS binaries
with the same interpreter problem, and they cannot be patched afterwards — Bazel checksums
its install base: `FATAL: corrupt installation: file '.../process-wrapper' is missing or
modified`. nixpkgs patches them at build time. Note this ignores `.bazelversion` (only
bazelisk reads it), so check the two agree after a nixpkgs bump.

**Dead ends, all measured**: `LD_LIBRARY_PATH` (Bazel doesn't pass it to `process-wrapper`);
`patchelf` on the helpers (install-base checksum, above); `/etc/ld.so.cache` (nixpkgs glibc
reads its cache from inside its own read-only store path, so ldconfig can't write one);
envfs (mounts `/usr/bin` and `/bin` only — it resolves _executable_ paths, never the ELF
interpreter, verified live on wyrm2).

**Outcome**: with the filesystem defaults in place, the Nix-built Haku sandbox image runs
`bazel test //...` at 25/26 — only the known no-Docker-socket e2e test fails. Full write-up:
[the Haku sandbox image notes](../../cluster/k8s/haku/workspaces/image/README.md).

## Status

| Issue              | Symptom                                                          | Fix                                                                              | Status      |
| ------------------ | ---------------------------------------------------------------- | -------------------------------------------------------------------------------- | ----------- |
| `/bin/bash`        | Ruff lint fails                                                  | nixpkgs `bazel_8` already patched                                                | Resolved    |
| Empty PATH         | Mypy lint fails (`env` not found)                                | `--action_env=PATH` in `~/.bazelrc`                                              | Applied     |
| `/usr/bin/ld.gold` | BuildBuddy toolchain fetch fails                                 | Patch `toolchains_buildbuddy` repo rule                                          | Not started |
| Scrubbed env (#4)  | `[nix-ld] FATAL: panicked … Posix(2)`; `sort: command not found` | Filesystem defaults: nix-ld fallback dir + a PATH-restoring `--shell_executable` | Resolved    |

Docker container test setup for reproducing these issues is at `devinfra/nixos_bazel_test/`.

## Key files

| File                                                      | Role                                      |
| --------------------------------------------------------- | ----------------------------------------- |
| `~/.bazelrc`                                              | User-local NixOS workarounds (issues 1-2) |
| `MODULE.bazel:336-349`                                    | BuildBuddy toolchain registration         |
| External `toolchains_buildbuddy+/rules.bzl:84-87`         | FHS symlinks that break on NixOS          |
| External `rules_mypy~/mypy/private/mypy.bzl:227`          | Action env missing PATH                   |
| External `rules_python` bootstrap (`interpreter_tmpl.sh`) | Uses bare `env` command                   |
