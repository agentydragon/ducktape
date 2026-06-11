# Rust Hook Daemon — Cutover Readiness Checklist

Before flipping the default to `rust`, validate on a live Claude Code web
session with `DUCKTAPE_CLAUDE_HOOK_IMPL=rust` set in the web UI env vars.

## Must pass (blocks cutover)

- [ ] Session starts without errors (no 500, no daemon timeout)
- [ ] `claude-hook --version` shows Rust binary (not Python wheel)
- [ ] `/web_selfcheck` skill passes all SPEC acceptance tests
- [ ] `env -0` under session env shows decrypted secrets
      (`BUILDBUDDY_API_KEY`, `GITHUB_TOKEN`, `DUCKTAPE_CI_READ_GITHUB_TOKEN`)
- [ ] `git --version` via shim works (passthrough)
- [ ] `git add -A` blocked with `BLOCKED` message (git shim policy)
- [ ] `bazelisk build //:hello` succeeds via shim
- [ ] `~/.kube/config` exists with correct server + token (bg command)
- [ ] `bbr test //devinfra/claude/...` completes (RBE works via bbr)
- [ ] Context banner visible in session transcript (`additionalContext`)
- [ ] Daemon log at expected path, no unhandled panics
- [ ] Second `SessionStart` (compaction) reuses existing daemon (no race)
- [ ] Idle watchdog fires after 30min (testable with short timeout override)

## Should verify (non-blocking but important)

- [ ] Daemon restart after SIGKILL (client kills stale, forks new)
- [ ] Circuit breaker blocks rapid re-fork after daemon crash
- [ ] `/mailbox` POST from bg command delivers to next REPL hook
- [ ] Multiple concurrent hook invocations → single daemon (daemon.lock)
- [ ] `curl --unix-socket $HOOK_DAEMON_SOCK http://localhost/health` returns ok
- [ ] Env file has 0o600 permissions

## How to run the live test

1. In Claude Code web UI → Settings → Environment Variables:
   set `DUCKTAPE_CLAUDE_HOOK_IMPL=rust`
2. Open a new session on `devel` (after this PR merges)
3. Setup hook fires → `web_setup.sh` reads the env var → installs
   `#claude-hooks-rs` flake output → Rust `claude-hook` on PATH
4. Walk the checklist above
5. Unset `DUCKTAPE_CLAUDE_HOOK_IMPL` to revert to Python
