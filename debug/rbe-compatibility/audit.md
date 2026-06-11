# RBE Compatibility Audit

## Goal

Determine the exact, narrow set of circumstances where `--remote_executor=""` is
legitimate, and replace the vague guidance in AGENTS.md with a precise guardrail.

## RBE Worker Environment

The RBE worker is `ghcr.io/agentydragon/rbe-worker` (devinfra/rbe_image/Dockerfile),
based on Ubuntu 24.04. Available tools:

| Tool          | Available? | Details                                       |
| ------------- | ---------- | --------------------------------------------- |
| `/bin/bash`   | Yes        | Ubuntu 24.04 base                             |
| Python 3      | Yes        | Via Nix flake devtools package                |
| Node.js       | Yes        | Via Nix flake devtools package                |
| Rust/Cargo    | Yes        | Via Nix flake devtools package                |
| Docker        | Yes        | Docker CE v28.1.0 with custom dockerd wrapper |
| Chromium deps | Yes        | Full X11 + Chromium library stack             |
| Xvfb          | Yes        | Virtual framebuffer                           |
| Nix           | Yes        | Single-user install                           |
| git           | Yes        | Including git-lfs                             |

**Key**: The worker image is a superset of what NixOS provides for build actions.
Any action that works locally on NixOS should also work on RBE. The worker has
standard FHS paths (`/bin/bash`, `/usr/bin/ar`, etc.) which NixOS lacks.

## Analysis: Build Action Categories

### Category 1: Normal compile/build/test actions

**Targets**: `//...` (the vast majority)

**RBE compatible**: YES

These are standard Bazel actions: compile Python, build JS bundles, run tests, etc.
The RBE worker has all required tools. `--incompatible_strict_action_env` ensures
the NixOS PATH doesn't leak to RBE, so actions get a clean `/bin:/usr/bin:/usr/local/bin`
PATH on the worker.

**Verdict**: Always use RBE. No exceptions.

### Category 2: Source-tree-writing workflows (bazel run)

These are NOT regular build actions. They are `bazel run` targets where the binary
executes locally and writes to the source tree. The **build** of these targets
uses RBE normally. The **run** always happens locally (that's how `bazel run` works).

#### 2a: Gazelle (`bb run //devinfra:gazelle`)

**What it does**: Reads source code, generates/updates `BUILD.bazel` files in the
source tree.

**Mechanics**: Gazelle is a Go binary. Bazel builds it (actions run on RBE), then
executes it locally. The local execution writes BUILD.bazel files.

**Does `--remote_executor=""` help?**: No. `bazel run`/`bb run` always executes the
binary locally regardless. The build of the Gazelle Go binary can and should use RBE.

**Verdict**: Use `bb run //devinfra:gazelle`. No `--remote_executor=""` needed.

#### 2b: Gazelle Python manifest (`bb run //devinfra:gazelle_python_manifest.update`)

**What it does**: Generates `devinfra/gazelle_python.yaml` from wheel metadata.

**Mechanics**: Same as Gazelle — binary runs locally, writes to source tree.

**Verdict**: Use `bb run //devinfra:gazelle_python_manifest.update`. No `--remote_executor=""` needed.

#### 2c: Python requirements (`//:requirements`)

**What it does**: Builds a `requirements.out` file from `pyproject.toml`.

**Current workflow** (from AGENTS.md):

```bash
bbr build //:requirements --remote_download_regex='.*requirements\.out' --noremote_accept_cached
cp bb-out/bazel-out/k8-fastbuild/bin/requirements.out requirements_bazel.txt
```

**RBE compatible**: YES. The build action runs on RBE, produces `requirements.out`.
Then the user manually copies it to the source tree.

**Verdict**: Already uses RBE. No `--remote_executor=""` needed.

#### 2d: Rust crate repin (`CARGO_BAZEL_REPIN=1`)

**What it does**: `CARGO_BAZEL_REPIN=1` is an environment variable that tells the
`crate.from_cargo` module extension to re-resolve dependencies from `Cargo.lock`
and regenerate `Cargo.Bazel.lock`.

**Mechanics**: This is a **module extension** (repo rule), not a build action. It
runs during `bazel build @crates//:all` as part of repository fetching. Module
extensions run locally on the Bazel client machine — they cannot run on RBE because
RBE executes spawn actions, not repository rules.

**Does `--remote_executor=""` help?**: Not for the repin itself (repo rules always
run locally). The flag would only affect the subsequent build of `@crates//:all`
targets. But those build actions should work fine on RBE since the worker has
Rust/Cargo available via Nix.

**Current docs say**: `CARGO_BAZEL_REPIN=1 bb build --remote_executor="" @crates//:all`

**Verdict**: The `--remote_executor=""` here is unnecessary. The repin (module
extension) always runs locally. The build actions can use RBE. Use:
`CARGO_BAZEL_REPIN=1 bazelisk build @crates//:all`

#### 2e: pnpm lockfile update

**What it does**: `update_pnpm_lock = True` in MODULE.bazel tells `npm_translate_lock`
to auto-update `pnpm-lock.yaml` when `package.json` files change.

**Mechanics**: This is also a module extension. Same as Rust repin — runs locally
on the Bazel client, not on RBE.

**Verdict**: No `--remote_executor=""` needed. Module extensions are always local.

### Category 3: Snapshot tests with syrupy

**How it works**: The `py_test` macro with `uses_syrupy = True` wires
`BazelAmberExtension` (<util/testing/bazel_snapshot_extension.py>). This extension
overrides `write_snapshot_collection` to copy each written `.ambr` file to
`TEST_UNDECLARED_OUTPUTS_DIR` when that env var is set (i.e. inside Bazel's test
sandbox). Bazel automatically uploads undeclared outputs from RBE runners.

**RBE workflow** (already works):

```bash
bbr test //path/to:snapshot_test \
  --test_arg=--snapshot-update --nocache_test_results

# Fetch updated snapshot from undeclared outputs:
cp bazel-testlogs/path/to/snapshot_test/test.outputs/snapshot_test.ambr \
   path/to/__snapshots__/snapshot_test.ambr
```

**Local workflow** (DX shortcut, one fewer copy step):

```bash
bazelisk test //path/to:snapshot_test \
  --test_arg=--snapshot-update --nocache_test_results \
  --remote_executor="" --config=nolint
# .ambr files written directly to source tree via runfiles symlinks
```

**Does `--remote_executor=""` help?**: It saves one copy step. When the test runs
locally, syrupy writes `.ambr` files through runfiles symlinks directly to the
source tree. On RBE, `BazelAmberExtension` copies them to undeclared outputs, which
Bazel downloads to `bazel-testlogs/` after the test completes — requiring a manual
`cp` back to the source tree.

This is purely a DX convenience. The RBE workflow is fully functional and correct.

**Verdict**: `--remote_executor=""` is never **required** for snapshot updates.
It's an optional shortcut. Both workflows produce identical results.

## Summary: Final Verdict

`--remote_executor=""` is **never required for correctness**. Every workflow in
this repo can run with RBE. The flag is at most a DX convenience for syrupy
snapshot updates (saves one `cp` step), and even that has a fully functional RBE
alternative via `BazelAmberExtension` + undeclared outputs.

### Why no workflow needs it

| Workflow                                           | Why `--remote_executor=""` is NOT needed                                    |
| -------------------------------------------------- | --------------------------------------------------------------------------- |
| `bb run //devinfra:gazelle`                        | `bazel run` always executes locally; build uses RBE                         |
| `bb run //devinfra:gazelle_python_manifest.update` | Same — binary runs locally                                                  |
| `//:requirements`                                  | Already documented to use RBE + download                                    |
| `CARGO_BAZEL_REPIN=1`                              | Module extension runs locally regardless; build actions use RBE             |
| `update_pnpm_lock`                                 | Module extension runs locally regardless                                    |
| Syrupy snapshot updates                            | `BazelAmberExtension` copies to undeclared outputs; fetch + cp works on RBE |
| Normal builds/tests                                | RBE is the default and correct choice                                       |
