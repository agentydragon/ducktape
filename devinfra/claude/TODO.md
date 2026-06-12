# claude_hooks TODO

## Session start simplification leftovers

The broad simplification work is complete; only these low-priority follow-ups
survived the deleted plan:

- Consider making `ci_env.sh` a fuller CI setup step (registry logins, release
  PAT handling) after auditing in-repo consumers of those env vars.
- Consider splitting the statusline hook into its own package if the duplicate
  home-manager + devshell installations become annoying enough to justify the
  closure churn.
- Consider caching `env_script_exports` alongside the profile instead of
  threading it through `app.state` -> `handle_session_start()` -> session env
  file. The current path works.
- Revisit `DUCKTAPE_DOCKER_CLIENT_KEY` once docker-ci is reachable from RBE
  workers; today `_common.sh` disables it, and `bbr.py` forwarding via
  `--remote_run_header` overrides `--test_env` in a way inner Bazel flags
  cannot unset.

## Dev environment consolidation leftovers

The repo dev tools are already shared through `flake.nix`/`packages.devtools`,
and Prettier now runs as a Nix-provided `language: system` pre-commit hook with
the Svelte plugin on `NODE_PATH`. Remaining low-priority work:

- Consider defining overlapping secret mappings once in Nix and generating a
  small `activate-secrets` script that decrypts SOPS sources and writes standard
  user paths such as `~/.config/bazel/buildbuddy.bazelrc` and `~/.kube/config`.
  Home-manager activation, `web_setup.sh`, and optionally the devshell shell hook
  could call the same script. Keep SSH keys and attic secrets in sops-nix, and
  keep the per-session Bazelrc overlay separate because proxy/JVM/platform
  settings are session-local.

## Decide whether the `bb` shim should inject the session bazelrc

The Rust `bazel`/`bazelisk` shims inject `--bazelrc=<session>/bazelrc` so
local Bazel gets the session truststore, BuildBuddy API key rc import, proxy
settings, and cache settings. The `bb` shim still passes through unchanged.

This may be wrong for direct local `bb build` / `bb test`, because `bb` runs a
local Bazel server and otherwise drops startup directives from implicit rc
files. It is not a trivial copy of the Bazel shim behavior because `bb` also
has `bb remote`/`bbr` modes and its own option parsing. Before changing it,
verify exactly where `--bazelrc=<session>` is accepted for local `bb`, remote
`bb`, and `bbr`, and make sure remote invocations do not get surprising local
session-only behavior.

## Rust parity follow-ups from the retired Python daemon

The Python daemon under `devinfra/claude/hook_daemon/` has been deleted. While
reading it before deletion, these behaviors looked potentially worth porting to
the Rust implementation later. Treat each item as a product decision, not an
automatic compatibility requirement.

- **Richer profile schema**: Python parsed `otel`, `bazel_bes_proxy`,
  `bes_nudge_remote_execution`, `setup_docker`, and `setup_tmpfs`. Rust ignores
  unknown fields today; promote any field we still want into
  `claude_hook/config.rs` with a test.
- **OpenTelemetry tracing**: Python wrote local JSONL spans and could export
  hook spans to OTLP with the profile `otel` endpoint and
  `DUCKTAPE_OTEL_BEARER_TOKEN`. Rust currently logs locally only.
- **BuildBuddy BES interceptor**: Python could proxy Bazel's BES stream over a
  UDS, forward it to BuildBuddy, and nudge the agent when a local Bazel run
  forgot remote execution. Rust only writes regular BuildBuddy bazelrc files.
- **Docker/supervisor setup**: Python could start or reuse a local Docker daemon,
  clean stale `/var/run/docker.pid`, set `DOCKER_HOST`, and surface Docker
  status in the session banner. Rust has no container-runtime setup path.
- **Tmpfs setup**: Python could mount tmpfs-backed session storage and add
  `startup --output_user_root=<session>/bazel-cache`. Rust only sizes Bazel's
  JVM heap based on detected gVisor/Firecracker signals.
- **Connectivity and platform diagnostics**: Python collected hostname, kernel,
  root filesystem type, PID 1 command line, Nix/nixpkgs availability, and a
  BuildBuddy reachability probe; Rust only has the minimal filesystem/container
  detection needed for Bazel JVM heap sizing.
- **Session context rendering**: Python rendered Mako templates with warnings,
  errors, startup-env output, Docker status, connectivity state, and profile
  `context_template`. Rust currently emits a fixed text banner and parses but
  does not render `context_template`.
- **Richer env file**: Python exported `SESSION_BAZELRC`,
  `DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR`, `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT`,
  `DUCKTAPE_SESSION_START_HOOK_TS`, `BBR_BAZELRC`, Docker env, and adjusted
  `NO_PROXY`/`no_proxy`. Rust exports only the startup env overlay,
  `env_exports`, and final shim `PATH` prepend.
- **WorktreeCreate handling**: Python handled `WorktreeCreate` by creating a git
  worktree and returning `worktreePath`. Rust models the hook but currently
  treats it as a noop.
- **Request environment persistence**: Python wrote the caller environment to
  `session_env.json` on every request for debugging. Rust does not persist the
  raw request env.
- **Structured error reporting**: Python's FastAPI middleware logged full
  unhandled exception tracebacks as JSON 500 responses. Rust currently reports
  most daemon failures through stderr logs and `{}` fallback from the client.

## Session banner warning when `is_gvisor()` is true

Sessions moved from gVisor to Firecracker microVMs (pre-2026-06 → now); the
hook's `is_gvisor()` detection survives only for JVM heap sizing. A gVisor
sighting today means the execution platform changed unexpectedly, so the hook
should surface a loud warning in the SessionStart banner ("running under
gVisor — not expected, tell the user") instead of silently picking the
smaller heap. See `docs/docker_evaluation_results.md` and the platform note
in `AGENTS.md`.

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

**Problem**: The `terraform_tflint` and `terraform_validate` pre-commit hooks (via `antonbabenko/pre-commit-terraform`) require tflint and opentofu on PATH. On Claude Code web sessions, these may not be available.

**Solution**: Consider auto-installing tflint and opentofu in the session start hook, so `pre-commit run` works out of the box for terraform changes.

## Benchmark `bb remote` with and without `--config=rbe`

With warm runner VMs, `bb remote` without `--config=rbe` (local `linux-sandbox` on the
runner) may be fast enough to skip RBE entirely. Measure on a nontrivial workload: dirty a
widely-imported file (e.g., a root `conftest.py` or a core library module) to invalidate
many targets, then compare `bb remote test //... --config=rbe` vs `bb remote test //...`
(no RBE). Check wall-clock time, action count, and cache hit rate.

## Integration Test: Session Start Hook via Nix devShell

**Problem**: The container E2E test
(`claude_hook/container_e2e/test_container_e2e.py`) exercises the Rust hook
daemon inside a Docker container, but does not exercise the Nix-packaged
devtools profile exactly as a live CLI session sees it. Missing Nix-level
runtime dependencies or packaging drift can still be discovered only when a
real CLI session starts.

**Solution**: Add an integration test that runs the exact session start hook
shim as configured in `.claude/settings.json` (i.e., invokes `claude-hook` the
same way Claude Code does), using the Nix devShell environment. This verifies
that all runtime imports resolve end-to-end — not just a subset.

## Container E2E Image From The Repo DevShell

**Problem**: The Claude hook container E2E image is a hand-assembled approximation
of the runtime environment. It currently bakes the specific test tools it needs
(`bazelisk`, `sops`, PyYAML, and a prepopulated Bazelisk Bazel binary cache), then
copies the `claude-hook` binary into the container at test time. That makes the
test cacheable and avoids network fetches, but it is not as faithful as testing in
a container with Nix installed and this repo's devShell realized.

**Solution**: Build a test image that has Nix plus the repo devShell installed, but
does **not** install or run the Claude hooks during image construction. The test
should still install/copy the hook under test at runtime, so it validates the
current build output rather than a preinstalled hook. This would naturally provide
`bazelisk` and Python runtime dependencies from the same toolchain live sessions
use.

Once that exists, remove the current ad hoc Bazelisk cache prepopulation from the
container E2E image. The nested `bazelisk build //:hello` should get Bazel from the
devShell/toolchain path, not from a manually maintained Bazelisk cache layout.
