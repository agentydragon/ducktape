@README.md

## Agent Instructions

- **Session start log**: `~/.cache/claude-hooks/session-start.log`
- **Supervisor logs**: `~/.config/claude-hooks/supervisor/supervisord.log` (supervisor daemon), `~/.config/claude-hooks/supervisor/auth-proxy.{log,err.log}` (auth proxy service)
- **gVisor environment**: Claude Code web runs on gVisor, not real Linux. Some syscalls behave differently.
- **9p filesystem limitation**: Root `/` is 9p. Supervisor uses TCP socket (`127.0.0.1:19001`) instead of Unix socket to avoid 9p hard link issues (EOPNOTSUPP).

## Debugging Commands

```bash
# Check session start log
tail -100 ~/.cache/claude-hooks/session-start.log

# Verify auth proxy connectivity
curl -s --max-time 5 -x http://127.0.0.1:18081 https://bcr.bazel.build/ | head -1

# Check Bazel configuration
cat ~/.cache/claude-hooks/auth-proxy/bazelrc

# Check supervisor status
python -m supervisor.supervisorctl -c ~/.config/claude-hooks/supervisor/supervisord.conf status
```
