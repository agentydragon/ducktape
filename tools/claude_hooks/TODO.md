# claude_hooks TODO

## Nix Installation Timeout

**Problem**: Installing nix on Claude Code web times out because downloading nixpkgs takes >2 minutes (session start hook timeout).

**Current Workaround**: The `claude_hooks` package is installed via `uv tool install` from a pre-built wheel (published to GitHub releases), avoiding Python dependency installation during session start. Terraform tools (opentofu) are Bazel-managed via `@multitool//tools/*`; tflint is installed by `antonbabenko/pre-commit-terraform` hook (requires tflint on PATH). Nix is installed separately for `nix eval`, flake operations, and `nix run nixpkgs#nixfmt` (used by pre-commit hook).

**Potential Solutions:** See <docs/nix-speed-options.md> for detailed analysis. Summary:

- **Pre-built nix store tarball** (recommended) - CI builds closure, publishes tarball, session hook unpacks
- **Pre-computed store paths** - CI records paths, session hook does `nix copy`

## Auto-install tflint in Session Start Hook

**Problem**: The `terraform_tflint` pre-commit hook (via `antonbabenko/pre-commit-terraform`) requires tflint on PATH. On Claude Code web (gVisor sandbox), tflint may not be available.

**Solution**: Consider auto-installing tflint in the session start hook (similar to how opentofu is handled), so `pre-commit run` works out of the box for terraform changes.

## gVisor Dockerfile Build as Claude Code Skill

Consider turning <docs/gvisor-dockerfile-build.md> into a Claude Code skill (`.claude/skills/`) so Claude can automatically apply the `podman run`+`podman commit` workaround when asked to build a Dockerfile, rather than requiring the agent to read the docs first.

## Supervisor Health Check Eventlistener

**Problem**: No proactive health monitoring for auth proxy - if it crashes, supervisor restarts it but we only notice on next bazel invocation.

**Solution**: Add custom eventlistener that:

- Runs every 60 seconds (TICK_60 event)
- Checks TCP port 18081 is listening
- Marks process FATAL if unreachable (supervisor auto-restarts)

Implementation outline:

```ini
[eventlistener:auth_proxy_health]
command=python3 -c "..."  # inline health check script
events=TICK_60
```

Script uses socket to test port, writes READY/RESULT per supervisor protocol.
