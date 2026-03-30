@README.md

## Agent Instructions

- **Hook daemon logs** (includes session start): `~/.claude/session-env/<session_id>/hook-daemon/daemon.log`
- **Supervisor logs**: `~/.claude/session-env/<session_id>/supervisor/supervisord.log` (supervisor daemon, used for container runtime)
- **Firecracker environment**: Claude Code web runs on Firecracker microVMs with a real Linux kernel (not gVisor). Root filesystem is ext4 on virtio disk, not 9p. See <web_env/docs/container_spec.md> for details.
- **Supervisor uses TCP socket**: `127.0.0.1:19001` (historical: originally to avoid 9p hard link issues, retained for simplicity).

## Debugging Commands

```bash
# Check hook daemon log (includes session start output)
tail -100 "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/hook-daemon/daemon.log"

# Verify auth proxy connectivity (AUTH_PROXY_URL is set in the session env file)
curl -s --max-time 5 -x "$AUTH_PROXY_URL" https://bcr.bazel.build/ | head -1

# Check session bazelrc
cat "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/bazelrc"

# Check supervisor status (container runtime only)
python -m supervisor.supervisorctl -c "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/supervisor/supervisord.conf" status
```
