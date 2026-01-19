@README.md

## Agent Instructions

- **Session start log**: `~/.cache/claude-code-web/session-start.log`
- **Supervisor logs**: `~/.config/supervisor/supervisord.log` (supervisor daemon), `~/.config/supervisor/bazel-proxy.{log,err.log}` (proxy service)
- **gVisor environment**: Claude Code web runs on gVisor, not real Linux. Some syscalls behave differently.
- **9p filesystem limitation**: Root `/` is 9p (including `~/.config`), only `/dev/shm` is tmpfs. Supervisord uses hard links for atomic socket creation which 9p doesn't support - supervisor will hang if its socket is on 9p. Use `CLAUDE_HOOKS_SUPERVISOR_DIR=/dev/shm/supervisor`.

## Debugging Commands

```bash
# Check session start log
tail -100 ~/.cache/claude-code-web/session-start.log

# Verify proxy connectivity
curl -s --max-time 5 -x http://127.0.0.1:18081 https://bcr.bazel.build/ | head -1

# Check Bazel configuration
cat ~/.cache/bazel-proxy/bazelrc
```
