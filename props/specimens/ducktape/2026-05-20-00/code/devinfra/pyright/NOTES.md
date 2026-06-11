# Python LSP (Pyright) in This Monorepo

## Status: Partially Working

The `pyright-lsp@claude-plugins-official` plugin is enabled
(`nix/home/claude_code/default.nix`) and Pyright 1.1.407 is installed via Nix.
The LSP provides basic code intelligence without any configuration:

- **Working**: `documentSymbol`, `hover` (stdlib + Nix-store third-party packages),
  `goToDefinition` (stdlib, pydantic, local intra-repo imports),
  `findReferences` (stdlib: 3881 refs for `json` across 1135 files)
- **Not working**: `workspaceSymbol` (empty), `findReferences` on non-stdlib symbols,
  `goToDefinition` on imports from Bazel-only packages

## The Gap

Pyright resolves packages through the Nix Python environment's `sys.path`
(133 site-packages). Bazel manages 454 packages via `@pypi//`. The ~300
Bazel-only packages (e.g. `fastmcp`, `mcp`, `openai`, `structlog`) won't
resolve — no hover, no goToDefinition, no type info for these.

There is no `pyrightconfig.json` — no excludes for `bazel-*`, no explicit
`pythonVersion`, no `extraPaths` for Bazel-managed packages.

## Package Resolution: How It Works Now

Pyright discovers packages through:

1. **Typeshed** (bundled) — stdlib type stubs
2. **`pythonPath` interpreter's `sys.path`** — Nix Python environment at
   `/nix/store/.../python3-3.13.12/`. This includes packages installed via
   Nix home-manager (pydantic, fastapi, rich, structlog, etc. — 133 total).
3. **Virtual environment discovery** — looks for `.venv/`, `venv/` etc.
   (none exists in this repo)
4. **`extraPaths` in config** — not configured

Bazel stores pip packages at
`<output_base>/external/rules_python++pip+pypi_313_<name>/site-packages/`
where `<output_base>` is machine-specific (e.g.
`~/.cache/bazel/_bazel_agentydragon/<hash>/`).

## Comparison with Rust LSP Setup

The Rust LSP uses rust-analyzer's `discoverConfig` mechanism (see
`devinfra/rust/LSP_NOTES.md`):

- rust-analyzer **calls our discover script itself** on startup
- `devinfra/rust/discover.sh` runs Bazel's `gen_rust_project` and outputs JSONL
- No manual step, no agent involvement, fully automatic
- Works because rust-analyzer has a native config file (`rust-analyzer.toml`)
  with `discoverConfig` support

**Pyright has no equivalent mechanism.** It reads `pyrightconfig.json` (or
`[tool.pyright]` in `pyproject.toml`) on startup and that's it. No hooks, no
discover commands, no dynamic config reloading.

## Options for Bridging Bazel Packages

### Option A: `extraPaths` via generated `pyrightconfig.json`

Adapt the approach from [agoessling/rules_pyright](https://github.com/agoessling/rules_pyright).

A script runs `bazel cquery //... --output starlark` with a Starlark snippet
that extracts `PyInfo.imports` from each target's providers, maps them to
`<output_base>/external/<name>/site-packages/`, and writes `extraPaths` into
`pyrightconfig.json`.

- **Pros**: Simple, battle-tested pattern, no system changes
- **Cons**: Needs a trigger to run the script (manual, hook, or direnv);
  `pyrightconfig.json` is machine-specific (gitignored)

### Option B: Synthetic venv with `.pth` file

Create `.venv/lib/python3.13/site-packages/_bazel_deps.pth` listing all Bazel
package site-packages directories (absolute paths). Pyright auto-discovers the
venv — no `pyrightconfig.json` needed for package resolution.

- **Pros**: Pyright auto-discovers `.venv`; no config file needed for packages
- **Cons**: Still needs machine-specific paths in the `.pth` file; `.venv/`
  is gitignored; still needs a trigger to generate

### Option C: `pythonPath` wrapper

Point pyright at a Python interpreter wrapper that injects Bazel's site-packages
into `sys.path`. Pyright would auto-discover packages through that interpreter.

- **Pros**: Pyright's native discovery, no `extraPaths`
- **Cons**: Wrapper needs to exist and be maintained; still needs generation

### Option D: Session start hook

Add generation to the existing Claude Code session start hook (alongside
`bazelisk warmup`). Every session gets fresh config automatically.

- **Pros**: Fully automatic, no manual step for Claude Code
- **Cons**: `bazel cquery //...` adds startup latency; only triggers for
  Claude Code sessions (not local editors)

## Existing Tool: `rules_pyright`

[agoessling/rules_pyright](https://github.com/agoessling/rules_pyright) is a
minimal Bazel extension (1k stars) that:

- Provides a `pyright_aspect` for running pyright as a Bazel action
- Has `tools/update_pyrightconfig.py` — the `bazel cquery` approach described
  in Option A
- Extracts `PyInfo.imports` via Starlark output format
- Maps imports to `<output_base>/external/` paths

Source at `~/code/rules_pyright/tools/update_pyrightconfig.py`.

## Claude Code LSP Limitations (same as Rust)

From `devinfra/rust/LSP_NOTES.md`:

- **No project-local LSP server config** — only marketplace plugins or `--plugin-dir`
- **Marketplace plugin config is read-only** — Nix-managed symlinks, no overrides
- **`workspace/configuration` returns null** — pyright falls through to
  `pyrightconfig.json` (which is why a repo-level config file works)
- **LSP plugin architecture**: `LSPServerManager` → lazy start on first `.py`
  file access → persistent server instance

## Key Decision: Trigger Mechanism

The main open question is **what triggers config generation**. Options:

1. **Manual**: Agent runs the script when needed. Simple but requires action.
2. **Session start hook**: Automatic for Claude Code. Adds startup latency.
3. **direnv**: Automatic on `cd`. Same latency, broader scope.
4. **Bazel-built venv**: One-time `bb run`, then pyright auto-discovers.
   Needs re-run when deps change.

Unlike Rust (where rust-analyzer triggers discovery itself), pyright gives us
no hook to ride on. Whatever we choose, it's external to pyright.

## TODO

- [ ] Decide on trigger mechanism
- [ ] Implement config generation script
- [ ] Create `pyrightconfig.json` with excludes and `extraPaths`
- [ ] Test all LSP operations with Bazel-only packages
- [ ] Add Claude Code command for LSP testing
- [ ] Test via `z-claude --print`
