# Ducktape

Personal infrastructure monorepo: configuration for **agentydragon** (ThinkPad), **gpd** (GPD Win Max 2), **vps**, **atlas** (Proxmox/Talos k8s).

## Build System

**Bazel** builds everything. Python 3.14+, Rust via Cargo/Bazel.

- **Python:** deps in `pyproject.toml`, used as `@pypi//pkg`; lockfile `requirements_bazel.txt` is generated (<devinfra/docs/lockfiles.md>). ruff + mypy run as Bazel aspects; `--config=nolint` skips them (<devinfra/docs/linting.md>).
- **Rust:** deps in root `Cargo.toml`, used as `@crates//name`; regenerate `Cargo.Bazel.lock` per <devinfra/docs/lockfiles.md>.
- **Gazelle** owns Python BUILD files; CI fails on drift. After adding/moving `.py` files or changing imports run `gazelle` (devshell; or `bb run //devinfra:gazelle`), check with `gazelle -mode=diff`. One `py_library` per `.py` file, referenced as `//pkg:module`; `__init__.py` stubs are generated via `imports = [".."]`. Conventions: [STYLE.md § Gazelle](STYLE.md#gazelle); mechanism: <devinfra/docs/gazelle.md>.
- **BuildBuddy** provides remote cache and RBE. `bbr` (wraps `bb remote`) runs the whole invocation on a remote runner; `bb run` keeps Bazel local and dispatches only actions (<AGENTS.md> § Bazel Commands). Two images: `ghcr.io/agentydragon/rbe-container-image` (<devinfra/rbe_container_image/Dockerfile>) is the action environment — its digest is in every action cache key, so it holds only what Bazel can't supply; `ghcr.io/agentydragon/buildbuddy-remote-runner` (<devinfra/buildbuddy_remote_runner/image.nix>, pinned in <devinfra/bbr.json>) adds Nix devtools for the outer invocation. Setup: <devinfra/setup_buildbuddy.sh>.

## Dotfiles

Nix home-manager in `nix/home/`; **never edit dotfiles in `~/`**. Deploy: <nix/README.md>.

## Development

```bash
nix shell .#precommit -c pre-commit install
nix shell .#precommit -c pre-commit run --all-files
bb run //devinfra/lint:buildifier
```

### Lint/Format Exclusions

`path/** rules-lint-ignored=true` in `.gitattributes`, plus `ruff.toml` `exclude` for Python. Other pre-commit gitattributes: `filename-conventions-ignored=true` (skip kebab-case check; default on for `cluster/`, `terraform/`, `tf/` and a few others), `cluster-manifest-ignored=true` (non-manifest YAML under `cluster/k8s/`).

## CI

GitHub Actions running `bazel {build,test}` (including lint) on a BuildBuddy remote runner — bazel-diff affected targets on PRs, `//...` on pushes and graph-wide changes — plus non-Bazel jobs (ansible-lint, nix, pre-commit, artifact publishing). See `.github/workflows/`.

## Conventions

- **`x/`** at any level: experimental, in-flux or one-off code; no stable API expected.
- **`TODO.md`**: persistent project-level TODOs; local ones stay inline. Remove entries when done.
- **`SPEC.md`**: the component's user-facing contract — what it guarantees, not how.
- **`archive/`**: only historical records carrying a future-relevant lesson; git history is the default archive.

### `plans/`

`<dir>/plans/`, or `<dir>/PLAN.md` for one central plan, holds future and in-progress design.

- A plan burns down: entries leave when their work lands; the plan is deleted when done (at most a short tombstone while a compatibility boundary needs a pointer).
- Nothing outside a plan cites one (code, `SPEC.md`, docs) — a stable citable identifier means the content is ready to graduate.
- Durable content graduates to the code site, `SPEC.md`, or `<dir>/docs/`. A goal the code doesn't meet yet stays plan content until the last step achieves it.

### `debug/`

`<project>/debug/<topic>.md`: an active investigation, in-progress RCA, or reproducible diagnostic procedure, under the project owning the behavior (`agentplane/debug/`, `cluster/debug/`, `devinfra/debug/`). Root `debug/` is for host-level investigations with no owning project (e.g. `debug/atlas/`) and cross-project ones — not a default inbox. One canonical report, linked from other projects. When resolved, promote durable lessons to the owner's docs (`cluster/docs/lessons_learned/` for cluster) and delete it. Same prose standard as any doc (<STYLE.md> § Documentation).

## License

AGPL 3.0
