# claude_hooks TODO

## Statusline: deduplicate usage display with GNOME extension

The statusline's subscription quota display (`7d:8%`) duplicates a GNOME
extension that already shows the same data. Options:

- **Config option** via XDG config file (`~/.config/claude-hooks/statusline.toml`
  or similar) to toggle individual sections (quota, daemon health, cost, etc.)
- **tmux statusline widget** — the statusline output is already plain text;
  a tmux `status-right` integration would be useful on remote/SSH sessions where
  GNOME isn't available. Consider if the GNOME extension + tmux widget cover
  the same ground, making the Claude Code statusline itself redundant.

## Consider reviving PostToolUse lint integration

Removed in <https://github.com/agentydragon/ducktape/pull/...> after
deciding the per-edit lint-and-revert flow was effectively spam — issues
get caught (and fixed by the same auto-apply hooks) when the agent runs
`pre-commit run` or commits via the `git` shim, which already invokes the
project's full pre-commit config. Running the same hooks on every Edit/Write
adds latency, can revert intentional changes the agent will fix later, and
duplicates work pre-commit-on-commit already does.

Reconsider if any of these change:

- Agent-driven edits frequently land bugs that the on-commit hooks catch
  too late (i.e. after a session worth of follow-on edits builds on broken
  state). If we observe this regressing meaningfully without per-edit lint,
  bring it back.
- Auto-apply hooks (`ruff-format` etc.) get expensive enough that running
  them once per edit is materially cheaper than once per commit on a wide
  changeset. (Unlikely — formatters scale well.)
- We add a non-formatting check (e.g. type errors, broken imports) that
  is cheap per-file and would meaningfully shorten the agent's iteration
  loop if surfaced immediately rather than at commit time.

What was removed: `post_tool_use.py`, `precommit_runner.py`, the
`PreCommitConfig` profile field, `templates/post_tool_use.mako`, and the
matching tests. Restoring would also restore the `pre-commit` runtime dep
on the wheel + Nix package.

## Nix Installation Timeout

**Problem**: Installing nix on Claude Code web times out because downloading nixpkgs takes >2 minutes (session start hook timeout).

**Current Workaround**: The `claude_hooks` package is installed via `uv tool install` from a pre-built wheel (published to GitHub releases), avoiding Python dependency installation during session start. Terraform tools (opentofu, tflint) are needed on PATH for `antonbabenko/pre-commit-terraform` hooks (`terraform_validate`, `terraform_tflint`). Nix is installed separately for `nix eval` and flake operations. Nix formatting uses a static nixfmt binary (no Nix dependency).

**Potential Solutions:**

- **Pre-built nix store tarball** (recommended) - CI builds closure, publishes tarball, session hook unpacks
- **Pre-computed store paths** - CI records paths, session hook does `nix copy`

## Auto-install Terraform Tools in Session Start Hook

**Problem**: The `terraform_tflint` and `terraform_validate` pre-commit hooks (via `antonbabenko/pre-commit-terraform`) require tflint and opentofu on PATH. On Claude Code web (gVisor sandbox), these may not be available.

**Solution**: Consider auto-installing tflint and opentofu in the session start hook, so `pre-commit run` works out of the box for terraform changes.

## Benchmark `bb remote` with and without `--config=rbe`

With warm runner VMs, `bb remote` without `--config=rbe` (local `linux-sandbox` on the
runner) may be fast enough to skip RBE entirely. Measure on a nontrivial workload: dirty a
widely-imported file (e.g., a root `conftest.py` or a core library module) to invalidate
many targets, then compare `bb remote test //... --config=rbe` vs `bb remote test //...`
(no RBE). Check wall-clock time, action count, and cache hit rate.

## Hook Daemon Lifecycle Management

**Problem**: The hook daemon client (`hook_daemon/client.py`) manually manages daemon lifecycle: pidfile read/write, process liveness checks, stale socket cleanup, fork+wait. This is ~50 lines of somewhat fiddly code.

**Potential solutions**:

- [`python-daemon`](https://pypi.org/project/python-daemon/) — handles server-side daemonization (double-fork, PID file, signal handling). Doesn't help with the client-side "ensure running" logic.
- [`zdaemon`](https://pypi.org/project/zdaemon/) — Zope-era daemon controller with start/stop/restart/status and PID management. Closest fit but adds a Zope dependency.
- **Move under supervisord** — adding the hook daemon under supervisor would eliminate the pidfile/fork logic entirely (client calls `supervisorctl start hook-daemon` if socket is dead). Trades custom lifecycle code for coupling to supervisor availability.

## Integration Test: Session Start Hook via Nix devShell

**Problem**: The container E2E test (`container_e2e/test_container_e2e.py`)
exercises the hook daemon inside a Docker container with `uv tool install`,
but does not exercise the Nix-packaged `claude-hooks` derivation. Missing
Nix-level dependencies (like `grpcio`) cause the daemon to crash with
`ModuleNotFoundError` at startup — only discovered when a real CLI session
starts.

**Solution**: Add an integration test that runs the exact session start hook
shim as configured in `.claude/settings.json` (i.e., invokes `claude-hook` the
same way Claude Code does), using the Nix devShell environment. This verifies
that all runtime imports resolve end-to-end — not just a subset.
