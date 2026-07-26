# NixOS Bazel compatibility issues

## Summary

Bazel doesn't work locally on NixOS. Three independent issues must be fixed — and a
fourth, [Issue 4](#issue-4-nix-ld-is-environment-based-and-bazel-rulesets-scrub-the-environment),
which cannot be fixed, only avoided.

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

## Issue 4: nix-ld is environment-based, and Bazel rulesets scrub the environment

**Symptom**: `[nix-ld] FATAL: panicked at src/main.rs:187:55: called 'Result::unwrap()' on
an 'Err' value: Posix(2)` (ENOENT) from an action, a test, or a genrule — not from anything
you invoked directly.

**Root cause**: nix-ld resolves the real loader by reading `NIX_LD` **from the environment**.
Bazel and its rulesets scrub environments as a hermeticity measure, and a scrubbed
environment has no `NIX_LD`, so nix-ld cannot find the interpreter and aborts. Each
scrubbing layer is separate and needs its own passthrough:

| Scrubber                                      | Seen as                                     | Passthrough that fixes it   |
| --------------------------------------------- | ------------------------------------------- | --------------------------- |
| `rules_python` `PyWriteBuildData`             | `exec env - …` under `--verbose_failures`   | `build --action_env=NIX_LD` |
| Test runner (does **not** inherit action env) | every `py_test` fails uniformly in ~0.5s    | `test --test_env=NIX_LD`    |
| `rules_js` `js_binary` wrapper                | esbuild lifecycle hook aborts (core dumped) | none found                  |

**Why it can't be fixed properly.** The list of scrubbers is open-ended — it is a property
of every ruleset you happen to depend on, discovered one build failure at a time. And the
three ways to make the binaries not need an environment are all closed:

| Attempt                          | Result                                                                                     |
| -------------------------------- | ------------------------------------------------------------------------------------------ |
| `LD_LIBRARY_PATH` in the image   | Bazel does not pass it to `process-wrapper`                                                |
| `patchelf` the extracted helpers | `FATAL: corrupt installation: … is missing or modified` — Bazel checksums its install base |
| `/etc/ld.so.cache`               | nixpkgs glibc reads its cache from inside its own read-only store path                     |

**envfs does not help, and never could.** It mounts `/usr/bin` and `/bin` only (verified in
the nixpkgs module), resolving _executable_ paths — shebangs, hardcoded `/bin/bash`. The
failing binaries have a valid path; what they cannot resolve is their **ELF interpreter**
`/lib64/ld-linux-x86-64.so.2` and `libstdc++.so.6`. That is nix-ld's job, not envfs's. (See
also the separate envfs rejection above, for a different reason.)

**Use nixpkgs' Bazel, not a bazelisk download.** Bazel's own extracted helpers
(`process-wrapper`, `linux-sandbox`) are FHS binaries with exactly this problem; nixpkgs
patches them at build time, so the install base it extracts is both Nix-correct and
self-consistent. Note this ignores `.bazelversion` — only bazelisk reads that file — so
check the two agree after any nixpkgs bump.

**Conclusion**: for a container, don't build the base from `dockerTools` and pile on env
plumbing. Use a normal-glibc (Debian) base carrying a Nix-built tool closure — the
[RBE image](../../devinfra/rbe_image/Dockerfile) shape — where downloaded binaries work with
no environment at all, and the tool list is still one reviewable Nix attribute set. Measured
end to end 2026-07-26 in
[the Haku sandbox image notes](../../cluster/k8s/haku/workspaces/image/README.md): Bazel
runs and `bazel run //cli:validate` passes, but `bazel test //...` dies in the JS toolchain.

## Status

| Issue              | Symptom                               | Fix                                                             | Status           |
| ------------------ | ------------------------------------- | --------------------------------------------------------------- | ---------------- |
| `/bin/bash`        | Ruff lint fails                       | nixpkgs `bazel_8` already patched                               | Resolved         |
| Empty PATH         | Mypy lint fails (`env` not found)     | `--action_env=PATH` in `~/.bazelrc`                             | Applied          |
| `/usr/bin/ld.gold` | BuildBuddy toolchain fetch fails      | Patch `toolchains_buildbuddy` repo rule                         | Not started      |
| Scrubbed env (#4)  | `[nix-ld] FATAL: panicked … Posix(2)` | Per-ruleset passthrough; unfixable in general — use an FHS base | Avoid, don't fix |

Docker container test setup for reproducing these issues is at `devinfra/nixos_bazel_test/`.

## Key files

| File                                                      | Role                                      |
| --------------------------------------------------------- | ----------------------------------------- |
| `~/.bazelrc`                                              | User-local NixOS workarounds (issues 1-2) |
| `MODULE.bazel:336-349`                                    | BuildBuddy toolchain registration         |
| External `toolchains_buildbuddy+/rules.bzl:84-87`         | FHS symlinks that break on NixOS          |
| External `rules_mypy~/mypy/private/mypy.bzl:227`          | Action env missing PATH                   |
| External `rules_python` bootstrap (`interpreter_tmpl.sh`) | Uses bare `env` command                   |
