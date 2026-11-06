# Rust LSP in This Monorepo

## Status and setup

Investigation observations (2026-05): hover, documentSymbol, workspaceSymbol and
goToDefinition worked in Claude Code; findReferences did not (suspected
project-json source-root limitation). These are historical results, not a fresh
compatibility check.

Two repo-specific settings make the Bazel crate graph available:

- Root [rust-analyzer.toml](../../rust-analyzer.toml) uses `workspace.discoverConfig`
  to run [discover.sh](discover.sh) at startup and when `BUILD.bazel` files change.
  This avoids fallback to root `Cargo.toml`, which exists only for
  `crate_universe` pinning and cannot build the workspace.
- [nix/home/home.nix](../../nix/home/home.nix) generates an empty
  `~/.cargo/config.toml`. The previous `rustc-wrapper = "sccache"` made
  `cargo metadata`'s sysroot discovery invoke `sccache rustc -vV`, failing with
  EPERM inside Claude Code's sandbox.

The script runs Bazel's `gen_rust_project` and emits JSONL progress followed by a
`finished` message containing the project inline. Its `buildfile` must be
absolute (`$PWD/WORKSPACE`); rust-analyzer panics on a relative path and uses its
parent to determine the project root. Native root-TOML loading needs neither
client `initializationOptions` nor `workspace/configuration` support.

Manual fallback:

```bash
bazelisk run @rules_rust//tools/rust_analyzer:gen_rust_project -- --config=nolint
```

The generated root `rust-project.json` is gitignored because it contains
machine-specific Bazel/Nix paths. The trailing `--config=nolint` is consumed by
`gen_rust_project` and passed to its inner crate-discovery build; it does not
configure the outer `bazelisk run`.

## Claude Code LSP limitations (as of 2026-05)

These findings came from the local restored-source investigation, not current
upstream validation. Plugin enablement lives in
[nix/home/claude_code/default.nix](../../nix/home/claude_code/default.nix).

- Server configuration came only from global marketplace plugins or the
  session-only `--plugin-dir` CLI flag. No project `.claude/lsp.json`, LSP entries
  in project settings, or project-directory plugin auto-discovery existed.
  `--plugin-dir` could not be supplied through settings or environment variables.
- Marketplace configuration was Nix-managed through read-only store symlinks,
  with no per-project override. The Rust plugin supplied no
  `initializationOptions`, `env` or `startupTimeout`.
- The client declared `workspace.configuration: false`; its fallback handler
  returned `null` for every item. rust-analyzer requested configuration anyway,
  then fell through to root `rust-analyzer.toml`.
- `LSPServerManager` → `config.ts` → `lspPluginIntegration.ts` →
  `LSPServerInstance.ts` loaded plugin configuration and initialized servers.
  Instances persisted in a `Map`, started lazily on first matching file access,
  and retried ContentModified (-32801) three times with exponential backoff.

## Investigation source pointers

Under `~/code/claude-code-sourcemap/restored-src/src/`:

- `services/lsp/`: server lifecycle and configuration flow above.
- `utils/plugins/schemas.ts`: `LspServerConfigSchema` supported
  `initializationOptions`, `env`, `startupTimeout`, `workspaceFolder` and `args`,
  though the marketplace entry used none.
- `utils/plugins/pluginLoader.ts`: `loadAllPluginsCacheOnly()` loaded marketplace,
  `--plugin-dir` and built-in plugins only.

Under `~/code/rust-analyzer/crates/`:

- `project-model/src/lib.rs`: file discovery checked `rust-project.json` before
  `Cargo.toml`, but workspace-root initialization preferred `Cargo.toml`.
- `rust-analyzer/src/config.rs`: root TOML loading;
  `linked_or_discovered_projects()` preferred `linkedProjects` over discovery.
- `rust-analyzer/src/discover.rs`: JSONL protocol and Path/Buildfile arguments.
- `rust-analyzer/src/handlers/notification.rs`: `filesToWatch` matched filenames
  only through `should_refresh_for_change`.

## TODO

- [ ] Re-evaluate when Claude Code adds project-local LSP configuration.
