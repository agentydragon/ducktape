@README.md

## Dependency Sync

The hook daemon's runtime Python dependencies are declared in **two places** that must stay in sync:

1. **Wheel `requires`**: `//:claude_hooks_wheel` in `BUILD.bazel` — used by `uv tool install` on Claude Code web
2. **Nix `propagatedBuildInputs`**: `claude-hooks` in `nix/packages/default.nix` — used by devShell and home-manager on NixOS

When adding or removing a runtime dependency, update **both** lists. A mismatch causes `ModuleNotFoundError` at daemon startup in whichever environment has the stale list. Both files have `SYNC:` comments pointing to each other.

## bb CLI Source

The `bb` CLI is open source at <https://github.com/buildbuddy-io/buildbuddy>. Remote Bazel logic lives in `cli/remotebazel/remotebazel.go` and `cli/storage/storage.go`. When debugging `bb remote` behavior (remote selection, flag semantics, git config keys), read the source directly.

## Agent Instructions

- **Hook daemon logs** (includes session start): `~/.claude/session-env/<session_id>/hook-daemon/daemon.log`
- **Hook daemon stderr log** (unhandled exceptions / tracebacks — check this first for SessionStart crashes): `~/.claude/session-env/<session_id>/hook-daemon/daemon.err.log`
- **Hook daemon startup failure marker** (written when the dispatcher couldn't reach the daemon at all): `~/.claude/session-env/<session_id>/hook-daemon/startup_failure.json`
- **Supervisor logs**: `~/.claude/session-env/<session_id>/supervisor/supervisord.log` (supervisor daemon, used for container runtime)
- **Platform detection**: Claude Code web runs on Firecracker microVMs (ext4 root, real Linux kernel). The session start hook detects the platform at runtime via `platform_detect.py`. See <web_env/docs/container_spec.md> for specs and IO benchmarks.
- **Supervisor uses TCP**: `127.0.0.1:19001` instead of Unix socket (historical: 9p hard link issues on gVisor, kept for compatibility).

## Container Lifecycle — Reverse-Engineered Source

Anthropic's `environment-manager` binary (Go, garble-obfuscated) is partially
reverse-engineered under <web_env/re/environment_manager/>. **Read this before
speculating about when/how session-start-adjacent things fire.** Specifically:

- **Which hook points fire on which session modes** (`new` / `resume` /
  `resume-cached` / `setup-only`): see
  <web_env/re/environment_manager/src/internal/envtype/anthropic/anthropic.go>
  `Initialize()`. Step 1 (install languages) and Step 2 (clone sources) are
  gated on `isNewOrSetup`. **Steps 3–6 run on every session** — including
  Step 3 (`runInitScript`, i.e. our `web_setup.sh`) and Step 4
  (`claude --init-only` which fires SessionStart hooks).
- **`process_api` (PID 1) lifecycle**, WebSocket ports, orphan monitor, OOM killers:
  <web_env/re/process_api/README.md>.
- **CLI flags and env vars** that tune the above:
  <web_env/re/SETUP_FLAGS_INVENTORY.md>.

If something in the lifecycle is unclear, grep
`devinfra/claude/web_env/re/environment_manager/src/` for the function name
or error string before opening an issue — there's likely already a decompiled
source file with line-annotated binary offsets.

## Debugging Commands

```bash
# Check hook daemon log (includes session start output)
tail -100 "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/hook-daemon/daemon.log"

# Check hook daemon stderr (tracebacks from unhandled exceptions in handlers)
tail -100 "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/hook-daemon/daemon.err.log"

# Check session bazelrc
cat "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/bazelrc"

# Check supervisor status (container runtime only)
python -m supervisor.supervisorctl -c "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/supervisor/supervisord.conf" status

# Check installed claude-hooks vs the pin in the working tree — catches
# "container reuse froze the wheel at an old pin" situations on Firecracker.
readlink /nix/var/nix/profiles/default/bin/claude-hook
python3 -c "import json; p=json.load(open('npins/sources.json'))['pins']['claude-hooks']; print(p['url'])"
```

If these disagree on the claude-hooks commit sha, the installed wheel has
drifted behind the pin — re-run `bash devinfra/claude/web_setup.sh` to pull
forward. See <docs/web-setup-debug.md> "Pin drift on persistent rootfs" for
the underlying cause.
