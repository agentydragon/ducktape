@README.md

## Agent Instructions

- **Session start log**: `~/.claude/session-env/<session_id>/session-start.log`
- **Hook daemon logs**: `~/.claude/session-env/<session_id>/hook-daemon/daemon.log` (hook daemon + in-process auth proxy)
- **Supervisor logs**: `~/.claude/session-env/<session_id>/supervisor/supervisord.log` (supervisor daemon, used for container runtime)
- **gVisor environment**: Claude Code web runs on gVisor, not real Linux. Some syscalls behave differently.
- **9p filesystem limitation**: Root `/` is 9p. Supervisor uses TCP socket (`127.0.0.1:19001`) instead of Unix socket to avoid 9p hard link issues (EOPNOTSUPP).

## Building Dockerfiles in gVisor

@docs/gvisor_dockerfile_build.md

## Debugging Commands

```bash
# Check session start log (SESSION_DIR is ~/.claude/session-env/<session_id>/)
tail -100 "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/session-start.log"

# Verify auth proxy connectivity
curl -s --max-time 5 -x http://127.0.0.1:18081 https://bcr.bazel.build/ | head -1

# Check session bazelrc
cat "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/bazelrc"

# Check hook daemon logs (auth proxy runs in-process)
tail -50 "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/hook-daemon/daemon.log"

# Check supervisor status (container runtime only)
python -m supervisor.supervisorctl -c "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/supervisor/supervisord.conf" status
```
