# Rust Claude Hook Specification

See <../README.md> for architectural and implementation details.

## Overview

Every Claude Code session is paired with a session-scoped Rust `claude-hook`
daemon. The command invoked by Claude Code reads the hook JSON from stdin,
starts the daemon if needed, sends the hook request over a Unix domain socket,
and prints the daemon's hook output back to stdout.

The daemon is scoped by Claude's session ID. Runtime files live under
`/tmp/claude-hd/<session_id>/`; the agent shell environment lives under
`~/.claude/session-env/<session_id>/`.

## Common Behaviors

These guarantees hold for both CLI and web profiles.

1. `SessionStart` loads the profile named by `DUCKTAPE_CLAUDE_HOOKS_PROFILE`
   relative to `CLAUDE_PROJECT_DIR`.
2. If the profile sets `startup_env_script`, the daemon sources it once at
   daemon startup and captures only the environment variables it adds or
   changes.
3. `SessionStart` writes `CLAUDE_ENV_FILE` with captured environment values,
   profile `env_exports`, and a final `PATH` prepend for the session shim
   directory.
4. The daemon installs self-contained Rust-backed shims for `bazelisk`,
   `bazel`, `bb`, and `bbr`. The `git` shim is installed only when at least
   one profile `git_shim` safety flag is enabled.
5. The `bazel` and `bazelisk` shims inject the session bazelrc and translate
   inherited `HTTP_PROXY` / `HTTPS_PROXY` values into JVM proxy properties for
   Bazel's Java clients. `bb` and `bbr` currently only resolve through to the
   real binaries.
6. If `BUILDBUDDY_API_KEY` is present in the startup environment overlay, the
   daemon writes a private `buildbuddy.bazelrc`, imports it from the session
   bazelrc, and enables `--config=rbe` plus `--shell_executable=/bin/bash`.
7. The daemon writes a `bbr.bazelrc` tagging BuildBuddy invocations with
   `ROLE=claude-code` and `TAGS=session:<session_id>`.
8. Profile `background_commands` run during `SessionStart`. Commands marked
   `after_env: true` source the generated env file before running.
9. Background command output and explicit `/mailbox` messages are queued and
   delivered to the model on the next REPL hook through `systemMessage`.
10. Mailbox output is not flushed on non-REPL hooks such as `SessionStart`,
    `Setup`, `WorktreeCreate`, and `ConfigChange`, because Claude Code only
    displays those outputs in the UI.
11. If the profile enables `idle_watchdog`, the daemon exits after 30 minutes
    with no hook, health, or mailbox requests.

## CLI Profile

The CLI profile expects the user's normal shell, Nix devshell, and direnv
configuration to provide credentials and tools. Its checked-in profile enables
the git safety shim for `git commit --amend` and `git add -A` / `git add .`,
leaves `git stash` unblocked for pre-commit compatibility, and exports a direnv
bridge so Bash tool calls pick up the cwd's `.envrc`.

## Web Profile

The web profile expects `devinfra/claude/web_setup.sh` to have installed the
Rust `claude-hook` binary, Python statusline, Nix tools, skills, and git
remotes before `SessionStart` fires. Its checked-in profile uses
`devinfra/secrets/web_env.sh` as `startup_env_script`, so the daemon captures
decrypted BuildBuddy, GitHub, CI-read, and OpenTelemetry credentials for the
agent environment.

Web-specific setup that still lives outside the Rust daemon, such as
kubeconfig materialization and fork-remote setup, runs as profile background
commands.

## Observable Acceptance Criteria

1. `claude-hook` starts a daemon for the current session and `curl
--unix-socket /tmp/claude-hd/<sid>/d.sock http://localhost/health` returns
   `{"status":"ok"}`.
2. `~/.claude/session-env/<sid>/sessionstart-hook-0.sh` exists after
   `SessionStart`, is mode `0600`, and places
   `~/.claude/session-env/<sid>/bin` first on `PATH`.
3. The generated session bin contains `bazelisk`, `bazel`, `bb`, and `bbr`;
   it contains `git` only when a git safety flag is enabled.
4. A `bazelisk build <target>` run from the generated environment uses the
   session bazelrc. When `BUILDBUDDY_API_KEY` is available, that bazelrc
   imports the private BuildBuddy rc and enables RBE.
5. `bbr` invocations import the generated `bbr.bazelrc` and are tagged with
   `session:<sid>` in BuildBuddy.
6. Background command output appears in the next REPL hook output as a
   `systemMessage`.
7. Daemon logs are present under `/tmp/claude-hd/<sid>/` and contain no
   unhandled errors from `SessionStart`.
8. CLI profile: `git commit --amend` and `git add -A` / `git add .` are
   blocked with a clear `[git-shim] BLOCKED` error; allowed git commands pass
   through to the real binary.
9. Web profile: the startup env script populates BuildBuddy and GitHub
   credentials; the kubeconfig background command writes a usable
   `~/.kube/config`.

## Non-Goals And Follow-Ups

The retired Python daemon implemented additional setup features that the Rust
daemon does not yet provide. Those are tracked as possible future work in
<../TODO.md>.
