@README.md

# Agent Guide for `wt`

## Environment and Tooling

See @../AGENTS.md for standard Bazel workflow (`bazel build --config=check //...`, `bazel test //...`).

Requirements: Bazel (via bazelisk), Python **3.13**+.

### Extra dependencies / binaries

- `libgit2` is provided via system packages or Nix.
- `gitstatusd` is provided by Bazel via `//third_party/gitstatusd` (test `data` dep). Tests discover it through runfiles automatically. The `config.gitstatusd_path` setting takes precedence over runfiles autodiscovery, which in turn takes precedence over PATH lookup.

## Development Commands

- Run the CLI entry point: `bazel run //wt:wt-cli -- --help`
- Tests: `bazel test //wt/...`
- Linting: `bazel build --config=lint //wt/...`
- Type checking: `bazel build --config=typecheck //wt/...`
- Format: `bazel run //tools/format`

Integration tests marked `integration` or `shell` spawn git repos and daemons; they may need
relaxed sandboxing to allow UNIX sockets and filesystem operations.

## Project Structure (src layout)

- `src/wt/cli.py` — CLI entry point (`wt` console script).
- `src/wt/client/` — CLI-side handlers, socket client, shell helpers.
- `src/wt/server/` — Background daemon, gitstatusd integration, worktree service logic.
- `src/wt/shared/` — Shared models, protocol definitions, constants.
- `src/wt/shell/` — Shell integration (`wt.sh`, installer).
- `tests/` — Pytest suite with fixtures for git repos, config factories, daemon/server tests.
- Additional docs: `docs/ARCHITECTURE.md`, `docs/SPLIT_FEATURE_DESIGN.md`, `docs/WORKTREE_IDEAS.md`.

## Testing and Markers

- Pytest configuration lives in `pyproject.toml`.
- Markers you will see:
  - `unit` — fast tests, no external processes.
  - `integration` — touches git repos/daemons.
  - `shell` — exercises fd3 shell integration (requires real shell support).
  - `real_github` — requires network access to GitHub (skipped by default).
  - `asyncio`, `slow` — opt-in markers for more specialized cases.
- Default addopts (already configured): `-m "not real_github" -v --tb=short --strict-markers --disable-warnings --timeout=30`.

## Operational Notes / Pitfalls

- The CLI communicates with a daemon via UNIX sockets. Tests that cover this path need filesystem
  - socket permissions; in restricted sandboxes those calls may fail with ECONNREFUSED or bind
    errors.
- Hooks and shell integration rely on fd3 semantics; avoid breaking this when modifying
  `wt.shell` utilities.
- `gitstatusd` metrics are surfaced to the client; when altering daemon startup flows, ensure
  `gitstatusd_listener` lifecycle stays in sync.
- Copy-on-write behavior differs per platform (clonefile vs reflink vs rsync). Keep feature flags
  configurable through the shared config models.

## Related Resources

- Top-level repository overview: `../AGENTS.md`
