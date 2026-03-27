# claude_hooks TODO

## Nix Installation Timeout

**Problem**: Installing nix on Claude Code web times out because downloading nixpkgs takes >2 minutes (session start hook timeout).

**Current Workaround**: The `claude_hooks` package is installed via `uv tool install` from a pre-built wheel (published to GitHub releases), avoiding Python dependency installation during session start. Terraform tools (opentofu, tflint) are needed on PATH for `antonbabenko/pre-commit-terraform` hooks (`terraform_validate`, `terraform_tflint`). Nix is installed separately for `nix eval` and flake operations. Nix formatting uses a static nixfmt binary (no Nix dependency).

**Potential Solutions:**

- **Pre-built nix store tarball** (recommended) - CI builds closure, publishes tarball, session hook unpacks
- **Pre-computed store paths** - CI records paths, session hook does `nix copy`

## Auto-install Terraform Tools in Session Start Hook

**Problem**: The `terraform_tflint` and `terraform_validate` pre-commit hooks (via `antonbabenko/pre-commit-terraform`) require tflint and opentofu on PATH. On Claude Code web (gVisor sandbox), these may not be available.

**Solution**: Consider auto-installing tflint and opentofu in the session start hook, so `pre-commit run` works out of the box for terraform changes.

## Sandbox Reminder Hook

Write a Claude Code hook that reminds the agent to use `dangerouslyDisableSandbox: true` when it runs `kubectl`, `systemctl`, `bazel`, `tofu`, `curl`, etc. inside the sandbox. Currently this is documented in root AGENTS.md but agents still forget.

## Supervisor Health Check Eventlistener

**Problem**: No proactive health monitoring for auth proxy - if it crashes, supervisor restarts it but we only notice on next bazel invocation.

**Solution**: Add supervisor `TICK_60` eventlistener that checks TCP port 18081 is listening and marks process FATAL if unreachable.

## Hook Daemon Lifecycle Management

**Problem**: The hook daemon client (`hook_daemon/client.py`) manually manages daemon lifecycle: pidfile read/write, process liveness checks, stale socket cleanup, fork+wait. This is ~50 lines of somewhat fiddly code.

**Potential solutions**:

- [`python-daemon`](https://pypi.org/project/python-daemon/) — handles server-side daemonization (double-fork, PID file, signal handling). Doesn't help with the client-side "ensure running" logic.
- [`zdaemon`](https://pypi.org/project/zdaemon/) — Zope-era daemon controller with start/stop/restart/status and PID management. Closest fit but adds a Zope dependency.
- **Move under supervisord** — the auth proxy already runs under supervisor. Adding the hook daemon there would eliminate the pidfile/fork logic entirely (client calls `supervisorctl start hook-daemon` if socket is dead). Trades custom lifecycle code for coupling to supervisor availability.
