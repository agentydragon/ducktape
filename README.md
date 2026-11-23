# Ducktape

Personal infrastructure monorepo: configuration for **agentydragon** (ThinkPad), **gpd** (GPD Win Max 2), and **atlas** (Proxmox/Talos k8s).

## Build System

**Bazel** builds everything. Python 3.14+, Rust via Cargo/Bazel.

- **Python:** deps in `pyproject.toml`, used as `@pypi//pkg`; lockfile `requirements_bazel.txt` is generated (<devinfra/docs/lockfiles.md>). ruff + mypy run as Bazel aspects; `--config=nolint` skips them (<devinfra/docs/linting.md>).
- **Rust:** deps in root `Cargo.toml`, used as `@crates//name`; regenerate `Cargo.Bazel.lock` per <devinfra/docs/lockfiles.md>.
- **Gazelle:** Run `gazelle` from the devshell after changing Python files or imports. See [regeneration and drift checks](devinfra/docs/gazelle.md) and [BUILD conventions](STYLE.md#gazelle).
- **BuildBuddy** provides remote cache and RBE. `bbr` (wraps `bb remote`) runs the whole invocation on a remote runner; `bb run` keeps Bazel local and dispatches only actions. Two images: `ghcr.io/agentydragon/rbe-container-image` (<devinfra/buildbuddy/images/rbe/Dockerfile>) is the action environment; `ghcr.io/agentydragon/buildbuddy-remote-runner` (<devinfra/buildbuddy/images/runner/image.nix>, pinned in <.bbrc>) adds Nix devtools for the outer invocation. Setup: <devinfra/buildbuddy/setup.sh>.

## Dotfiles

Nix home-manager in `nix/home/`; **never edit dotfiles in `~/`**. Deploy: <nix/README.md>.

## Development

See [pre-commit setup, formatting, and exclusions](devinfra/docs/linting.md).

## CI

GitHub Actions run `bazel {build,test}` (including lint) via `bb remote` on a BuildBuddy remote runner — bazel-diff selects affected targets on PRs, `//...` on pushes and graph-wide changes — plus non-Bazel jobs (ansible-lint, nix, pre-commit, artifact publishing). See [where Bazel CI runs tests and why](devinfra/ci/README.md#where-bazel-ci-runs-tests) and [the workflows](.github/workflows/).

## Conventions

- **`x/`** at any level: experimental, in-flux or one-off code; no stable API expected.
- **`TODO.md`**: persistent project-level TODOs; local ones stay inline. Remove entries when done.
- **`SPEC.md`** records the component's user-facing guarantees. Update it when the contract changes. See the [Claude hook specification](devinfra/claude/claude_hook/SPEC.md) for an example.
- **`archive/`**: only historical records carrying a future-relevant lesson; git history is the default archive.

Before deleting a finished plan or resolved investigation, move any workaround that still applies or any fragility warning beside the affected code. Link to an upstream issue or commit when useful. When retiring an obsolete workaround, leave at most a one-line warning where the code remains fragile.

### `plans/`

`<dir>/plans/`, or `<dir>/PLAN.md` for one central plan, holds active work only: outstanding implementation, open decisions and acceptance criteria. Plans are not historical records or stable references.

- A plan burns down: entries leave when their work lands; delete completed or superseded plans. Consolidate overlapping plans under one active owner.
- Code, `SPEC.md` and maintained docs cite enduring documentation, not a plan. `TODO.md` may link an active plan for dispatch; remove that pointer when the plan closes.
- Move enduring content such as contracts and proofs to the code site, `SPEC.md`, or `<dir>/docs/`. A goal the code doesn't meet yet stays plan content until the last step achieves it.
- Capture non-obvious lessons from failed approaches as maintained constraints or proofs, then delete the obsolete plan. Git and PRs retain the chronology and old validation logs.

### `debug/`

Keep active investigations, in-progress RCAs, and reproducible diagnostic procedures under `<project>/debug/` in the project that owns the behavior. Use root `debug/` for cross-project investigations and host-level investigations with no owning project, such as `debug/atlas/`.

Keep one canonical report and refer to it by link from other projects, code comments, and PRs. When the investigation is resolved, promote durable lessons to the owner's docs (`cluster/docs/lessons_learned/` for cluster) and delete the report. Follow the [documentation style](STYLE.md#documentation).

## License

AGPL 3.0
