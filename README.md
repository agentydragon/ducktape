# Ducktape

Personal infrastructure monorepo. Manages configuration for: **agentydragon** (ThinkPad), **gpd** (GPD Win Max 2), **vps**, **atlas** (Proxmox/Talos k8s).

## Build System

**Bazel** is the unified build system. Python 3.13+, Rust via Cargo/Bazel.

### Python

- Deps: add to `pyproject.toml`, regenerate the lockfile via <devinfra/docs/lockfiles.md>, use `@pypi//pkg` in BUILD
- Lockfile: `requirements_bazel.txt` (never edit manually)
- Lint: ruff + mypy via Bazel aspects (default on; `--config=nolint` to skip)

### Gazelle

Python BUILD files are gazelle-managed. After adding, moving, or renaming a `.py`
file or changing imports, run gazelle rather than hand-editing managed `srcs`/`deps`:

```bash
bb run //devinfra:gazelle              # Update BUILD files
bb run //devinfra:gazelle -- --mode=diff  # Verify convergence (no diff, no errors)
```

One `py_library` per `.py` file (no aggregators). Reference `//pkg:module` not `//pkg`.
Bazel auto-generates `__init__.py` stubs via `imports = [".."]`. Conventions:
[STYLE.md § Gazelle](STYLE.md); mechanism and escape hatches:
<devinfra/docs/gazelle.md>.

### Rust

Add deps to root `Cargo.toml`, regenerate `Cargo.Bazel.lock` via
<devinfra/docs/lockfiles.md>, then use `@crates//crate_name` in BUILD deps.

### Remote Cache + RBE

BuildBuddy provides remote caching and remote build execution (RBE). Build actions run on BuildBuddy runner VMs; results are cached so unchanged targets are instant on repeat runs. `bbr` (a wrapper around `bb remote`) runs the whole invocation on a runner; `bb run` keeps Bazel local and dispatches only build actions — which to reach for is in <AGENTS.md> § Bazel Commands.

Two images, deliberately separate. `ghcr.io/agentydragon/rbe-worker`
(<devinfra/rbe_image/Dockerfile>) is the execution platform actions run inside; its digest is in
every action's cache key, so it carries only what Bazel cannot supply from the repo it is building.
`ghcr.io/agentydragon/bbr-runner` (<devinfra/bbr_runner/Dockerfile>) adds the Nix devtools for the
`bb remote` runner and is pinned only in <devinfra/bbr.json>. Setup: <devinfra/setup_buildbuddy.sh>.

## Dotfiles

Managed by Nix home-manager in `nix/home/`. **Do NOT edit dotfiles in `~/`**.

Deploy: see <nix/README.md> (NixOS hosts use `sudo nixos-rebuild switch`; standalone non-NixOS configs use `home-manager switch`).

## Development

```bash
pre-commit install  # Installs ruff, buildifier, rustfmt, prettier, etc.
```

### Lint/Format Exclusions

Exclusions require two files (pre-commit reads `.gitattributes`, ruff reads `ruff.toml`):

1. Add `path/** rules-lint-ignored=true` to `.gitattributes`
2. If Python, also add to `ruff.toml exclude`

Other gitattributes consumed by pre-commit checks:

- `filename-conventions-ignored=true` — skips kebab-case filename enforcement
  (defaulted on for `cluster/`, `terraform/`, `tf/`, and a few other trees
  where kebab-case is conventional).
- `cluster-manifest-ignored=true` — for YAML files under `cluster/k8s/` that
  aren't K8s manifests (e.g. `rules_distroless` apt manifests next to a
  CronJob image's `BUILD.bazel`). The cluster validator skips them from
  orphan detection and resource parsing.

## CI

- **GitHub Actions + `bbr`**: `bazel {build,test} //...` via `bbr` (remote Bazel on BuildBuddy RBE, includes lint)
- **GitHub Actions (non-Bazel)**: ansible-lint, nix, pre-commit, artifact publishing (wheels, container images)

See `.github/workflows/`.

## Common Commands

```bash
bb run //devinfra/lint:buildifier    # Format Bazel files
```

Lockfile and generated manifest workflows: <devinfra/docs/lockfiles.md>.

## Conventions

### `x/` — Experimental

`x/` subdirectories (e.g. `x/bsc/`, `finance/augur/x/`) mark experimental, in-flux, or one-off code that hasn't stabilized. Any directory at any level can have an `x/` subfolder. Don't expect stable APIs or finished design from code under `x/`.

### `TODO.md`

`<dir>/TODO.md` tracks persistent project-level TODOs. Inline code comments are fine for TODOs local to a specific location; cross-cutting or project-wide items go in `TODO.md`. Remove entries once fully completed.

### `plans/`

`<dir>/plans/` holds future work and work-in-progress design notes; a component with one
central plan uses `<dir>/PLAN.md` instead (e.g. `loom/PLAN.md`, `haku/PLAN.md`).

**A plan is a burn-down**: an entry _leaves_ when its work lands (never parked as done),
and the whole plan is deleted once fully done — at most a short tombstone while an
active compatibility boundary needs a pointer.

**Nothing outside a plan may cite one** — no code comment, `SPEC.md`, or doc pointing at
a plan's numbered requirement or step: plans are ephemeral, so such a citation either
pins the entry permanently or dangles. Citing a doc is fine — needing a stable citable
identifier is the signal that content is ready to graduate out of the plan.

**Durable content goes somewhere durable**: the invariant at the code site, the
guarantee in `SPEC.md`, the design in a doc under `<dir>/docs/`. Graduating (parts of)
a plan into one or more docs is the normal end of plan content, not a failure of the
plan. The goal the plan exists to reach _is_ plan content — a rule the code does not
hold to yet leaves with the last step that achieves it. The test is whether the
statement outlives the work.

### `debug/`

`<dir>/debug/<topic>.md` holds an active investigation, RCA work in progress, or a reproducible
diagnostic procedure. Delete it when resolved, or promote its durable lesson to the current docs.
The `cluster/` subproject uses `cluster/docs/lessons_learned/` instead. A `debug/` note is held
to the same prose standard as everything else (<STYLE.md> § Documentation) — being an
investigation is not a licence for padding or after-the-fact justification.

### `archive/`

Existing `<dir>/archive/` files are historical records that survived because they carry a
future-relevant lesson. Do not use the directory as a parking lot for completed plans or incident
archaeology; Git history is the default archive.

### `SPEC.md`

`<dir>/SPEC.md` is the high-level, user-facing specification of what a component guarantees. An outside observer should be able to read it to understand the component's contract without reading the implementation. Keep it at the "what it promises" level — implementation details belong in README.md or the code.

## License

AGPL 3.0
