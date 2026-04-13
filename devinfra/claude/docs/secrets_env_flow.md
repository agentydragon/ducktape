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
        │   → Decrypts all SOPS secrets into daemon os.environ
        │   → Side effect: writes ~/.kube/config from K8S_TOKEN
        │
        ├── Session starts (SessionStart hook fires)
        │   ├── Reads secrets from os.environ
        │   ├── Writes kubeconfig from K8S_TOKEN
        │   ├── Configures BuildBuddy from BUILDBUDDY_API_KEY
        │   ├── Sets up fork remote from GITHUB_TOKEN
        │   └── Writes session env file including env_overlay block
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

| Consumer                 | Gets secrets from                           |
| ------------------------ | ------------------------------------------- |
| kube MCP server          | `kube_from_sops.sh` (self-decrypts)         |
| Cloud MCP servers        | Anthropic-managed — no local secrets needed |
| Hook daemon subprocesses | session env file (via startup_env_script)   |
| Claude Code process      | session env file (sourced by env hooks)     |

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

**Result**: Claude and everything it spawns (hook daemon, tool calls) sees these vars.
`SOPS_AGE_KEY` set here is available to the hook daemon's decryption.

### 2. Internal Anthropic config → `claude --init-only` subprocess only

Flow:

1. Sessions API response: `environment.environment_variables` (internal Anthropic config)
2. `anthropic.go:Initialize()` → `claude.RunInit(..., e.config.EnvironmentVariables)`
3. `RunInit()` runs `claude --init-only` with `syscall.Environ()` plus these vars appended

**Result**: `claude --init-only` sees the full container environment plus
`anthropicConfig.EnvironmentVariables`. The init script (`web_setup.sh`) does **not**
receive these — it runs before `claude --init-only` via `process.ExecuteScript` and
only inherits the container env. Neither subprocess receives the user's UI vars.

## startup_env_script: The Decryption Hook

`ProfileConfig.startup_env_script` is the sole decryption path on web:

- Configured in `profiles/web/profile.yaml` as `devinfra/secrets/web_env.sh`
- Run by `main.py` at daemon startup, after the profile is loaded
- By daemon startup, `SOPS_AGE_KEY` is in the inherited env → decryption works
- `eval "$(web_env.sh)" && env -0` captures exported vars, diffs against
  initial `os.environ`, and merges new/changed vars into `os.environ`
- All subsequent session start logic reads secrets directly from `os.environ`

## CLI Profile

CLI sessions don't use `startup_env_script`. Secrets are sourced via `.envrc`
(direnv) before the daemon starts: `eval "$(devinfra/secrets/cli_env.sh)"`.
By the time the daemon launches, all secrets are already in the environment.
