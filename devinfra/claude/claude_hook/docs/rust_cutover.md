# Rust Claude Hook Cutover

This records the historical cutover from the Python Claude Code hook daemon to
the Rust `claude-hook` binary. The active implementation lives in
`devinfra/claude/claude_hook/`.

## Status

| Phase                                                           | Status   |
| --------------------------------------------------------------- | -------- |
| 1. Container E2E contract test                                  | **Done** |
| 2. Kubeconfig extraction to standalone script                   | **Done** |
| 3. Rust scaffolding + client dispatch + double-fork             | **Done** |
| 4. SessionStart parity (env, shims, bg cmds, lifecycle, banner) | **Done** |
| 5. Release pipeline + flake wiring                              | **Done** |
| 6. Cutover to Rust and delete Python daemon                     | **Done** |

The Python daemon has been deleted. Future parity work found while retiring it
is tracked in `devinfra/claude/TODO.md`.

## Release Pipeline

- `release.yml` builds `//devinfra/claude/claude_hook:claude_hook` and
  publishes it to GitHub Releases with tag `claude-hook-rs-<12hex>`.
- `sync-pins.yml` auto-updates `nix/artifact-pins.json` every 30 minutes.
- `nix/packages/default.nix` exposes `claude-hook-rs`, a static binary
  derivation installed as `$out/bin/claude-hook`.
- The `#devtools` flake output includes the Rust `claude-hook-rs` binary and
  the Python statusline package. The old Python `claude-hooks` wheel now
  contains statusline code only.

## Live Validation

Open a new session on `devel` after hook changes land and validate with
`/web_selfcheck`. For manual spot checks:

- `claude-hook --version` shows the Rust binary.
- `curl --unix-socket /tmp/claude-hd/<sid>/d.sock http://localhost/health`
  returns `{"status":"ok"}`.
- The generated env file under `~/.claude/session-env/<sid>/` is mode `0600`
  and prepends its `bin` directory to `PATH`.
- `git --version` via shim passes through, while `git add -A` is blocked when
  the git safety policy is enabled.
- `bazelisk build //:hello` uses the session bazelrc through the shim.
- `bbr test //devinfra/claude/...` works through BuildBuddy.
- Background command output reaches the next REPL hook through `/mailbox`.
- A second `SessionStart` reuses the existing daemon.
- Daemon logs under `/tmp/claude-hd/<sid>/` contain no unhandled panics.

## Remaining Gaps

- Rust parses `context_template` but does not render per-profile context
  templates.
- Rust does not export OpenTelemetry spans.
- The daemon grandchild currently `exec`s itself for a clean process image.
  Since the Tokio runtime is not created before fork, it could call the daemon
  entrypoint directly, or the client and daemon could become separate binaries.
- The Rust tests cover the primary path, but some restart/crash edge cases from
  the deleted Python tests still deserve direct Rust coverage.
