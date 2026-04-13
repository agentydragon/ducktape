# Secrets & Env Var Flow in Claude Code Web Sessions

How secrets (API keys, tokens) and user-facing environment variables reach
Claude Code and its subprocesses in a web session.

## The Problem

`SOPS_AGE_KEY` is a pod-level container env var — available to all processes
from the moment the container starts, including `web_setup.sh`. Both decryption
paths therefore work reliably.

The need for **two paths** is not about secret availability — it's about which
consumers can read from which sources:

- **`settings.local.json["env"]`** is injected by Claude Code into MCP server
  processes. Hook subprocesses do NOT read from it.
- **Session env file** (written by `SessionStart`) is sourced by hook subprocesses.
  MCP servers never see it.

The old approach used only `settings.local.json` and added a per-secret
`_decrypt_sops_secret()` fallback directly in the session handler — duplicated
logic that was fragile to maintain. The `startup_env_script` path eliminates
that fallback: secrets are decrypted once into `os.environ` at daemon startup
and flow naturally into the session env file.

## Current Architecture

Two parallel paths decrypt the same secrets for different consumers:

```
Container starts (SOPS_AGE_KEY in container env from the start)
│
├── environment-manager runs setup via `claude --init-only`: web_setup.sh
│   ├── Installs Nix + devtools (sops now on PATH)
│   ├── Runs web_env.sh → decrypts all SOPS secrets
│   └── Writes settings.local.json:
│       { "env": { "DUCKTAPE_CLAUDE_HOOKS_PROFILE": "...",
│                  "BUILDBUDDY_API_KEY": "...",          ← for pre-commit
│                  "GITHUB_TOKEN": "...", ... } }
│
└── environment-manager starts Claude Code (interactive)
    │   Claude Code reads settings.local.json["env"] and injects it into:
    │     - All hook subprocesses (including the hook daemon)
    │     - MCP server processes  ← only path that reaches them
    │   (kube MCP server self-decrypts via kube_from_sops.sh — not listed here)
    │
    └── First hook dispatch → `claude-hook` entrypoint → Hook daemon starts
        │   (inherits SOPS_AGE_KEY + settings.local.json["env"] from Claude Code)
        ├── Loads profile (profiles/web/profile.yaml)
        │
        ├── Runs startup_env_script: devinfra/secrets/web_env.sh
        │   → Decrypts all SOPS secrets into daemon os.environ
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

**`claude --init-only`** (runs `web_setup.sh`):

- Inherits environment-manager's full process env via `syscall.Environ()` — includes
  `SOPS_AGE_KEY` from the k8s pod secret, `HTTPS_PROXY`, and all other container-level vars
- Plus `anthropicConfig.EnvironmentVariables` (from the `environment.environment_variables`
  API field) appended as `key=value` pairs
- Does **not** receive `startup_context.environment_variables` (the user's UI vars)

RE evidence: `claude/init.go:RunInit` starts `claude --init-only` with
`cmd.Env = syscall.Environ()` then appends `anthropicConfig.EnvironmentVariables`.

**Claude Code** (interactive session):

- Inherits environment-manager's full process env via `os.Environ()` — same container
  env as above
- Plus `startup_context.environment_variables` (user's "Environment Variables" UI knob)
  appended as `key=value` pairs
- Plus fixed vars: `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_SESSION_ID`,
  `CLAUDE_CODE_REMOTE_SESSION_ID`, `CLAUDE_CODE_ENVIRONMENT_RUNNER_VERSION`,
  `CLAUDE_CODE_DIAGNOSTICS_FILE`

RE evidence: `claude/claude_code_executor.go:Execute` starts with `envVars = os.Environ()`
then appends `e.Config.EnvironmentVariables` (= `StartupContext.EnvironmentVariables`).
`cmd/cmd_task_run.go` creates the executor with `parsedCtx.StartupContext` after
`Manager.Run()` returns.

### Why two paths?

`settings.local.json` and `startup_env_script` serve different consumers:

| Consumer                 | Gets env from                                   | Path                            |
| ------------------------ | ----------------------------------------------- | ------------------------------- |
| kube MCP server          | `kube_from_sops.sh` (self-decrypts at startup)  | `claude-sandbox-kubectl-mcp.sh` |
| Other MCP servers        | `settings.local.json["env"]`                    | web_setup.sh                    |
| Hook daemon subprocesses | session env file                                | startup_env_script              |
| Claude Code process      | `settings.local.json["env"]` + session env file | both                            |

The kube MCP server (`claude-sandbox-kubectl-mcp.sh`) is self-sufficient: it
calls `kube_from_sops.sh` to decrypt the token and write a temp kubeconfig,
runs `kubernetes-mcp-server` as a subprocess (not exec), then the EXIT trap
deletes the temp file. `CLAUDE_SANDBOX_K8S_TOKEN` is no longer needed in
`settings.local.json` or anywhere in the env.

`~/.kube/config` is written by `web_env.sh` (as a side effect, via
`kube_from_sops.sh`) so that bare `kubectl` commands Claude runs also work
without `KUBECONFIG` being set. Web sessions have no pre-existing user
kubeconfig so this is safe.

Both paths use `web_env.sh` with `SOPS_AGE_KEY` available and `sops` on PATH
(installed in Step 2 of `web_setup.sh`). The `web_setup.sh` call uses `|| true`
for robustness — if it fails unexpectedly, hook subprocesses still work via
the session env file, but MCP servers would lose their secrets. The
`startup_env_script` path forwards any failure as a warning to session mailboxes.

## Two Separate `environment_variables` Maps

There are two distinct `environment_variables` maps in the session, sourced from
different places and delivered to different processes:

### 1. User's "Environment Variables" UI knob → Claude Code process only

These come from the sessions API response: `startup_context.environment_variables`.

Flow:

1. Sessions API: `GET /v1/sessions/<id>/context` → `startup_context.environment_variables`
2. `v1_parser.go:buildStartupContext()` → `config.StartupContext.EnvironmentVariables`
3. `cmd/cmd_task_run.go` creates `ClaudeCodeExecutor{Config: parsedCtx.StartupContext}` after `Manager.Run()`
4. `claude_code_executor.go:Execute()` → appended to Claude Code subprocess env

**Result**: Claude and everything it spawns (hook daemon calls, tools) sees these vars.
`SOPS_AGE_KEY` set here is available to Claude and the hook daemon's tool calls.

### 2. Internal Anthropic config → setup script subprocess only

These come from: `environment.environment_variables` in the same API response.

Flow:

1. Sessions API response: `environment.environment_variables` (internal Anthropic config)
2. `v1_parser.go:buildEnvironmentResponse()` → raw JSON for `anthropicConfig`
3. `anthropic.go:Initialize()` → `claude.RunInit(..., e.config.EnvironmentVariables)`
4. `RunInit()` (RE: `claude/init.go`) runs `claude --init-only` with `syscall.Environ()`
   (full process env, including `SOPS_AGE_KEY` from k8s pod secret) plus these vars appended

**Result**: The setup script sees the full container environment plus
`anthropicConfig.EnvironmentVariables`. It does **not** receive
`startup_context.environment_variables` (the user's UI vars).
This is why `SOPS_AGE_KEY` set via the "Environment Variables" UI knob is **not**
available during `web_setup.sh` — the UI vars go to path 1 (Claude Code), not here.

## startup_env_script: The Right Hook

`ProfileConfig.startup_env_script` solves the timing problem cleanly:

- Configured in `profiles/web/profile.yaml` as `devinfra/secrets/web_env.sh`
- Run by `main.py` at daemon startup, after the profile is loaded
- By daemon startup, SOPS_AGE_KEY is in the container env → decryption works
- `eval "$(web_env.sh)" && env -0` captures the exported vars, diffs against
  initial env, and merges new/changed vars into `os.environ`
- All subsequent session start logic reads secrets directly from `os.environ`

## What Lives Where

| Secret                          | `settings.local.json` (for MCP)                                           | Session env file (for hooks)                                   |
| ------------------------------- | ------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `BUILDBUDDY_API_KEY`            | web_setup.sh                                                              | startup_env_script (reliable)                                  |
| `GITHUB_TOKEN`                  | web_setup.sh                                                              | startup_env_script (reliable)                                  |
| `K8S_TOKEN`                     | web_setup.sh                                                              | startup_env_script (reliable)                                  |
| `CLAUDE_SANDBOX_K8S_TOKEN`      | n/a — MCP launcher decrypts directly                                      | n/a — `~/.kube/config` written by web_env.sh                   |
| `DUCKTAPE_OTEL_BEARER_TOKEN`    | web_setup.sh                                                              | startup_env_script (reliable)                                  |
| `DUCKTAPE_CI_READ_GITHUB_TOKEN` | web_setup.sh                                                              | startup_env_script (reliable)                                  |
| `DUCKTAPE_CLAUDE_HOOKS_PROFILE` | web_setup.sh (**required** — daemon needs this before startup_env_script) | n/a                                                            |
| User env vars (UI knob)         | n/a                                                                       | Sessions API `startup_context` → Claude Code only              |
| Setup-script env vars           | n/a                                                                       | Sessions API `environment` → setup script (`--init-only`) only |
| `SOPS_AGE_KEY`                  | n/a (k8s container env, available to all)                                 | n/a                                                            |

## CLI Profile

CLI sessions don't use `startup_env_script`. Secrets are sourced via `.envrc`
(direnv) before the daemon starts: `eval "$(devinfra/secrets/cli_env.sh)"`.
By the time the daemon launches, all secrets are already in the environment.
