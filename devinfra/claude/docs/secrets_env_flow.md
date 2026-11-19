# Secrets & Env Var Flow in Claude Code Web Sessions

How secrets (API keys, tokens) and user-facing environment variables reach
Claude Code and its subprocesses in a web session.

## Why Decryption Only Happens in the Hook Daemon

`SOPS_AGE_KEY` is set via the Claude Code web UI "Environment Variables" knob
(`startup_context.environment_variables`). This path delivers the key only to
the interactive Claude Code process and its subprocesses — **not** to the init
script (`web_setup.sh`) or to `claude --init-only`, both of which run before the
interactive session and receive only the container env (plus `anthropicConfig.EnvironmentVariables`
for `claude --init-only`), but not the user's UI vars.

As a result, `web_setup.sh` cannot decrypt SOPS secrets. Decryption happens
exclusively in the hook daemon via `startup_env_script`.

**Why this architecture?** Storing secrets as SOPS-encrypted files in the repo means
rotation is a single `sops` edit — no pasting new values into multiple UIs. Without
hook-daemon decryption, each secret would need to be entered separately into every
consumer (Claude Code web UI environment variables, other frontends). With it, a single
`SOPS_AGE_KEY` in the Claude Code web UI is the only credential to manage there.

## Current Architecture

```
Container starts
│
├── environment-manager runs web_setup.sh (init script, direct bash via ExecuteScript)
│   ├── Installs Nix + devtools (sops now on PATH)
│   └── Adds github-no-proxy remote + bbr config
│       (inherits container env only; SOPS_AGE_KEY not available — no decryption)
│
├── environment-manager runs `claude --init-only` (fires Setup+SessionStart hooks)
│   (non-fatal; these hooks fire again when the interactive session starts)
│
└── environment-manager starts Claude Code (interactive)
    │   Inherits container env + user UI vars (including SOPS_AGE_KEY)
    │
    └── First hook dispatch → `claude-hook` entrypoint → Hook daemon starts
        │   (inherits SOPS_AGE_KEY from Claude Code process env)
        ├── Loads profile (profiles/web/profile.yaml)
        │
        ├── Runs startup_env_script: devinfra/secrets/web_env.sh
        │   → Decrypts SOPS secrets; vars it adds or changes become the env overlay
        │   → The daemon's own env is not modified
        │
        ├── Session starts (SessionStart hook fires)
        │   ├── Writes session env file: overlay exports + profile env_exports
        │   ├── Configures BuildBuddy from the overlay's BUILDBUDDY_API_KEY
        │   └── Launches background commands with the overlay in their env
        │       (kubeconfig writes ~/.kube/config)
        │
        └── Subsequent hook subprocesses
            └── Source session env file → have all secrets
```

### Env vars received by each subprocess

The following is based on best-effort RE of the `environment-manager` binary (Build ID
`495ea204`) — see `web_env/re/environment_manager/src/` for RE source. The RE is a
reconstruction and may have gaps; the running binary is the ground truth.

**Init script** (`web_setup.sh`, run by environment-manager via `process.ExecuteScript`):

- Inherits environment-manager's process env (container env)
- Does **not** receive `anthropicConfig.EnvironmentVariables` (only appended to `claude --init-only`)
- Does **not** receive `startup_context.environment_variables` (user's UI vars)
- Therefore: `SOPS_AGE_KEY` (a UI var) is **not** available here

**`claude --init-only`** (run by environment-manager after the init script):

- Inherits environment-manager's full process env via `syscall.Environ()`
- Plus `anthropicConfig.EnvironmentVariables` (internal Anthropic config field)
- Does **not** receive `startup_context.environment_variables` (user's UI vars)
- Therefore: `SOPS_AGE_KEY` (a UI var) is **not** available here either

**Claude Code** (interactive session):

- Inherits environment-manager's full process env via `os.Environ()`
- Plus `startup_context.environment_variables` (user's "Environment Variables" UI knob)
  — this includes `SOPS_AGE_KEY` and `DUCKTAPE_CLAUDE_HOOKS_PROFILE`
- Plus fixed vars: `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_SESSION_ID`, etc.

### What reaches each consumer

| Consumer                 | Gets secrets from                                          |
| ------------------------ | ---------------------------------------------------------- |
| kube MCP server          | `kubeconfig.py` background command (self-decrypts)         |
| Cloud MCP servers        | Anthropic-managed — no local secrets needed                |
| Hook daemon subprocesses | env overlay; `after_env` ones source the session env file  |
| Claude Code process      | session env file (sourced by env hooks)                    |
| Agent `Bash` tool calls  | inherit from Claude Code process env (e.g. `GITHUB_TOKEN`) |

`settings.local.json` is no longer written by `web_setup.sh` — it was always
written empty because `SOPS_AGE_KEY` was unavailable at setup time.

## Two Separate `environment_variables` Maps

There are two distinct `environment_variables` maps in the session, sourced from
different places and delivered to different processes.

### 1. User's "Environment Variables" UI knob → Claude Code process only

Flow:

1. Sessions API: `GET /v1/sessions/<id>/context` → `startup_context.environment_variables`
2. `v1_parser.go:buildStartupContext()` → `config.StartupContext.EnvironmentVariables`
3. `cmd/cmd_task_run.go` creates `ClaudeCodeExecutor{Config: parsedCtx.StartupContext}`
4. `claude_code_executor.go:Execute()` → appended to Claude Code subprocess env

### 2. Internal Anthropic config → `claude --init-only` subprocess only

Flow:

1. Sessions API response: `environment.environment_variables` (internal Anthropic config)
2. `anthropic.go:Initialize()` → `claude.RunInit(..., e.config.EnvironmentVariables)`
3. `RunInit()` runs `claude --init-only` with `syscall.Environ()` plus these vars appended

## startup_env_script: The Decryption Hook

`ProfileConfig.startup_env_script` is the sole decryption path on web:

- Configured in `profiles/web/profile.yaml` as `devinfra/secrets/web_env.sh`
- Sourced by `run_daemon` in <devinfra/claude/claude_hook/main.rs> at daemon startup,
  after the profile is loaded
- By daemon startup, `SOPS_AGE_KEY` is in the inherited env → decryption works
- `source web_env.sh && env -0` captures exported vars and diffs them against the
  daemon's env; the new/changed vars are the env overlay, and the daemon's own env
  is not modified (<devinfra/claude/claude_hook/env_script.rs>)
- Session start passes the overlay to the session env file, the session bazelrc's
  BuildBuddy credential, and background commands

## CLI Profile

CLI sessions don't use `startup_env_script`. Secrets are sourced via `.envrc`
(direnv) before the daemon starts: `source devinfra/secrets/cli_env.sh`.
By the time the daemon launches, all secrets are already in the environment.
