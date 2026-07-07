# Lockfile Workflows

Generated dependency files are owned by their generators. Do not edit
`requirements_bazel.txt`, `Cargo.Bazel.lock`, or `pnpm-lock.yaml` by hand.

## Python Requirements

Add Python dependencies to `pyproject.toml`, then regenerate the Bazel
requirements output through RBE:

```bash
bbr build //:requirements --remote_download_regex='.*requirements\.out' --noremote_accept_cached
cp bb-out/bazel-out/k8-fastbuild/bin/requirements.out requirements_bazel.txt
bb run //devinfra:gazelle_python_manifest.update
```

On a NixOS host without `/bin/bash`, the workspace Bazel rc may need the host
shell override for the Gazelle manifest update:

```bash
bb run //devinfra:gazelle_python_manifest.update \
  --config=nolint --norun_validations --shell_executable="$(command -v bash)"
```

`--config=nolint` and `--norun_validations` avoid the ruff lint aspect in a
sandbox that lacks coreutils; they are for this manifest update path, not a
general validation shortcut.

## Rust Crates

Add Rust dependencies to the root `Cargo.toml`, then repin:

```bash
CARGO_BAZEL_REPIN=1 bazelisk build @crates//:all
```

Use `@crates//crate_name` in `BUILD.bazel` deps.

## JavaScript Packages

Add dependencies to `package.json`, run the relevant Bazel build once so the
managed pnpm lock update is produced, then run it again after `pnpm-lock.yaml`
has changed.

Do not run raw `pnpm install`; Bazel manages pnpm for this repo.
