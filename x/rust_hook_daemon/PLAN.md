# Rust Hook Daemon v0

Rewrite the Claude Code hook daemon (`devinfra/claude/hook_daemon/`) in Rust.
Single static binary, wire-compatible with Claude Code's JSON protocol.

Reference implementation: `devinfra/claude/hook_daemon/` (Python, ~6k LOC).
Rust implementation: `devinfra/claude/claude_hook/` (12 modules, ~4.3k lines).

## Status

| Phase                                                           | Status      |
| --------------------------------------------------------------- | ----------- |
| 1. Container E2E contract test                                  | **Done**    |
| 2. Kubeconfig extraction to standalone script                   | **Done**    |
| 3. Rust scaffolding + client dispatch + double-fork             | **Done**    |
| 4. SessionStart parity (env, shims, bg cmds, lifecycle, banner) | **Done**    |
| 5. Release pipeline + flake wiring + `--impl` switch            | **Done**    |
| 6. Cutover (flip default, delete Python daemon)                 | Not started |

Both `python` and `rust` parameterizations of the container E2E pass the
same assertion set on RBE.

## Release pipeline

- `release.yml` matrix entry builds `//devinfra/claude/claude_hook:claude_hook`
  and publishes to GitHub Releases with tag `claude-hook-rs-<12hex>`.
- `sync-pins.yml` auto-updates `npins/sources.json` every 30min.
- `nix/packages/default.nix` has `claude-hook-rs` derivation (static binary,
  no runtime deps; installs as `$out/bin/claude-hook`).

## Flake outputs

```
#devtools       → devToolsCommon + Python claude-hooks wheel (default)
#devtools-rust  → devToolsCommon + Rust claude-hook-rs binary
```

`web_setup.sh --impl=<python|rust>` selects which output to install.
Default is `python`. Both remove the other profile before installing.

## Testing the Rust impl live

No branch needed. The Setup hook (`web_setup_hook.sh`) runs as a `claude`
subprocess and inherits user-UI env vars. Set
`DUCKTAPE_CLAUDE_HOOK_IMPL=rust` in the Claude Code web UI environment
settings; `web_setup.sh` reads it, removes the default `#devtools`
profile, and installs `#devtools-rust` instead. Open a session, validate
with `/web_selfcheck`. Unset the var (or set to `python`) to revert.

Two parallel Claude Code web environments — one with the var set, one
without — give you side-by-side testing on the same branch.

## Remaining gaps

1. **Per-profile context template** (Mako→Tera). Test profile doesn't use it.
2. **Real PreToolUse / PostToolUse handlers**. Both return noop in Rust.
3. **OpenTelemetry tracing**. Deferred.
4. **Skip re-exec in double-fork**. The grandchild currently `exec`s
   itself (`claude-hook daemon --sock --daemon-dir`) for a clean process
   image. Since the tokio runtime hasn't been created before fork, the
   grandchild could call `run_daemon()` directly — saves ~1ms and one
   exec. Alternatively, split into separate client/daemon binaries.

Cutover readiness checklist: <CUTOVER_CHECKLIST.md>

## Next goal

**Phase 6 — cutover**: flip `web_setup.sh` default to `rust`, delete
Python daemon code. Gate on all "must pass" items above being verified.
