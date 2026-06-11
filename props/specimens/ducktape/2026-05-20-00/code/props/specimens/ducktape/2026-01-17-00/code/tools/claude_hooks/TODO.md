# claude_hooks TODO

## Nix Installation Timeout

**Problem**: Installing nix on Claude Code web times out because downloading nixpkgs takes >2 minutes (session start hook timeout).

**Current Workaround**: Binary tools (alejandra, cluster tools) are installed via direct binary downloads in `binary_tools.py`. Nix is still installed for `nix eval` and flake operations, but tool installation via `nix profile install` is avoided.

**Potential Solutions** (if we want to use nix for more tools):

- Investigate if flakes have optimizations to avoid downloading all of nixpkgs
- Check if there's a way to use a minimal/shallow nixpkgs fetch
- Consider pre-cached nix store or binary cache closer to Claude Code web infra
- Look into `nix profile install` with `--no-eval-cache` or similar flags

## Supervisor Health Check Eventlistener

**Problem**: No proactive health monitoring for pproxy - if it crashes, supervisor restarts it but we only notice on next bazel invocation.

**Solution**: Add custom eventlistener that:

- Runs every 60 seconds (TICK_60 event)
- Checks TCP port 18081 is listening
- Marks process FATAL if unreachable (supervisor auto-restarts)

Implementation outline:

```ini
[eventlistener:bazel_proxy_health]
command=python3 -c "..."  # inline health check script
events=TICK_60
```

Script uses socket to test port, writes READY/RESULT per supervisor protocol.
