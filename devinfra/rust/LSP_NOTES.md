# Rust LSP in This Monorepo

## Status: Working

rust-analyzer resolves the Bazel crate graph via `rust-project.json`. Confirmed
working in Claude Code: hover shows type info, documentSymbol parses structures,
workspaceSymbol finds symbols across all crates.

Working features: hover, documentSymbol, workspaceSymbol, goToDefinition
Not working: findReferences (likely a project-json source root limitation)

## Setup (two changes required)

1. **`rust-analyzer.toml`** (checked into repo root) — uses
   `workspace.discoverConfig` to auto-generate `rust-project.json` via Bazel
   on startup and when `BUILD.bazel` files change. Prevents rust-analyzer from
   falling back to `Cargo.toml` (which is only for `crate_universe` pinning).

2. **No sccache in `~/.cargo/config.toml`** — the previous `rustc-wrapper =
"sccache"` setting caused `cargo metadata` (used for sysroot discovery) to
   invoke `sccache rustc -vV`, which fails with EPERM inside Claude Code's
   sandbox. Since this repo builds Rust via Bazel (not cargo), sccache in the
   global cargo config was only harmful. Removed via Nix (`nix/home/home.nix`).

## How it works: discoverConfig

The `workspace.discoverConfig` in `rust-analyzer.toml` configures an external
command that rust-analyzer invokes to generate `rust-project.json` on demand.
This replaces the `linkedProjects` approach with automatic regeneration.

**Flow**:

1. rust-analyzer starts, finds no linked projects, invokes the discover command
2. `devinfra/rust/discover.sh` runs `bazelisk run @rules_rust//tools/rust_analyzer:gen_rust_project`
3. The script outputs JSONL progress messages, then a `Finished` message with
   the full `rust-project.json` content embedded inline
4. rust-analyzer loads the project data and begins indexing
5. When a `BUILD.bazel` file changes, rust-analyzer re-invokes the discover command

**Advantages over `linkedProjects`**:

- No manual regeneration step — rust-analyzer triggers it automatically
- Re-generates when `BUILD.bazel` files change
- No stale `rust-project.json` sitting around after dependency changes

**The discover command JSONL format** (from rust-analyzer's `discover.rs`):

```jsonl
{"kind":"progress","message":"Generating rust-project.json via Bazel..."}
{"kind":"finished","buildfile":"WORKSPACE","project":{...full rust-project.json content...}}
```

## Manual generation (fallback)

If `discoverConfig` isn't working, you can still generate manually:

```bash
bazelisk run @rules_rust//tools/rust_analyzer:gen_rust_project -- --config=nolint
```

Creates `rust-project.json` at the workspace root (~741KB, 439 crates).
The `--config=nolint` flag skips mypy/ruff checks during build analysis.
The file is in `.gitignore` — it contains machine-specific absolute paths
(Bazel cache dirs under `~/.cache/bazel/`, Nix store paths).

## Why this works

Claude Code's `rust-analyzer-lsp` plugin starts rust-analyzer with `rootUri`
pointing at the repo root. Without `rust-analyzer.toml`, rust-analyzer finds
`Cargo.toml` and runs `cargo check --workspace`, which fails (the Cargo.toml
isn't a real workspace — it only exists for `rules_rust`'s `crate_universe`
dependency pinning).

The `discoverConfig` in `rust-analyzer.toml` overrides auto-discovery.
rust-analyzer loads this file directly from the workspace root regardless of
client capabilities — it doesn't need `initializationOptions` or
`workspace/configuration` support from the LSP client.

This only works because rust-analyzer has its own local config file mechanism
independent of the LSP client. For language servers without such a mechanism,
there would be no way to configure them per-project in Claude Code.

## Claude Code LSP limitations (as of 2026-05)

Investigated via the leaked Claude Code source at
`~/code/claude-code-sourcemap/restored-src/src/services/lsp/`:

- **No project-local LSP server config.** LSP servers are only configurable
  through marketplace plugins (global) or `--plugin-dir` CLI flag (session-only).
  There is no `.claude/lsp.json`, no LSP config in project `settings.json`, and
  no auto-discovery of plugins from the project directory.

- **`--plugin-dir` is CLI-only.** Cannot be set from project settings or env vars.
  Would need to be passed on every `claude` invocation.

- **Marketplace plugin config is read-only.** The `claude-plugins-official`
  marketplace config is Nix-managed (symlinks into `/nix/store`). The
  `rust-analyzer-lsp` entry has no `initializationOptions`, `env`, or
  `startupTimeout` — and there's no per-project override mechanism.

- **`workspace/configuration` returns null.** Claude Code's LSP client declares
  `capabilities.workspace.configuration: false` but still registers a fallback
  handler that returns `null` for every config item. rust-analyzer sends
  `workspace/configuration` requests anyway (ignoring the capability), receives
  nulls, and falls through to `rust-analyzer.toml` — which is why our workaround
  works.

- **LSP plugin architecture**: `LSPServerManager` → `config.ts` (loads plugins)
  → `lspPluginIntegration.ts` (extracts LSP configs) → `LSPServerInstance.ts`
  (starts server, sends `initialize`). Servers are persistent (kept in a `Map`),
  started lazily on first `.rs` file access, and retried on ContentModified
  errors (code -32801, 3 retries with exponential backoff).

## Files

- `rust-analyzer.toml` — `workspace.discoverConfig` pointing to discover script
- `devinfra/rust/discover.sh` — wrapper that runs Bazel gen_rust_project, outputs JSONL
- `rust-project.json` — generated Bazel crate graph (gitignored, machine-specific)
- `Cargo.toml` — root manifest for `crate_universe`, NOT for building
- `~/.cargo/config.toml` — Nix-managed, intentionally empty (no sccache)
- `nix/home/home.nix` — generates empty `~/.cargo/config.toml`
- `nix/home/claude_code/default.nix` — enables `rust-analyzer-lsp` plugin

## Investigation history

- Claude Code's LSP plugin source: `~/code/claude-code-sourcemap/restored-src/src/services/lsp/`
- LSP config schema: `~/code/claude-code-sourcemap/restored-src/src/utils/plugins/schemas.ts`
  (`LspServerConfigSchema` supports `initializationOptions`, `env`, `startupTimeout`,
  `workspaceFolder`, `args` — but the marketplace plugin uses none of these)
- Plugin loader: `~/code/claude-code-sourcemap/restored-src/src/utils/plugins/pluginLoader.ts`
  (`loadAllPluginsCacheOnly()` loads from marketplace + `--plugin-dir` + builtins only)
- rust-analyzer project discovery: `~/code/rust-analyzer/crates/project-model/src/lib.rs`
  (checks `rust-project.json` before `Cargo.toml` for file paths, but workspace
  root initialization prefers `Cargo.toml` — hence the config override)
- rust-analyzer config loading: `~/code/rust-analyzer/crates/rust-analyzer/src/config.rs`
  (reads `rust-analyzer.toml` from workspace root, `linkedProjects` and `discoverConfig`
  are global configs, `linked_or_discovered_projects()` at line 2260 prefers
  `linkedProjects` over discovered projects)
- rust-analyzer discover command: `~/code/rust-analyzer/crates/rust-analyzer/src/discover.rs`
  (spawns command, parses JSONL output, `DiscoverArgument` supports Path/Buildfile args)
- rust-analyzer file watching: `~/code/rust-analyzer/crates/rust-analyzer/src/handlers/notification.rs`
  (`filesToWatch` matches on filename only via `should_refresh_for_change`)

## TODO

- [ ] Re-evaluate when Claude Code adds project-local LSP config support
