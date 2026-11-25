# Secrets and environment variables in Claude Code web sessions

Enter `SOPS_AGE_KEY` in the web UI's **Environment Variables**. Rotate the individual
credentials in their SOPS files. The interactive session's hook daemon uses the key
to populate the tools that need those credentials.

## Which process receives the UI variables?

The reconstructed `environment-manager` for build `495ea204` distinguishes the
user's `startup_context.environment_variables` from Anthropic's internal
`environment.environment_variables`. Every process below inherits the container
environment. Whether each initialization step runs depends on the session mode and
binary build. [Build-specific startup evidence](../web_env/re/environment_manager/README.md#session-startup-by-build)

| Process                                 | Additional environment                       | Consequence                                                                           |
| --------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------- |
| Configured init script (`web_setup.sh`) | Container environment only                   | Can install Nix and devtools before a UI key is available.                            |
| `claude --init-only`                    | Internal environment map passed to `RunInit` | Its hooks also start without the UI key.                                              |
| Interactive `claude`                    | UI variables and session metadata            | Its children inherit `SOPS_AGE_KEY` and the selected `DUCKTAPE_CLAUDE_HOOKS_PROFILE`. |

The [session parser](../web_env/re/environment_manager/src/internal/input/v1_parser.go)
separates the two maps. The [initializer](../web_env/re/environment_manager/src/internal/envtype/anthropic/anthropic.go)
passes the internal map to `RunInit`. The [Claude executor](../web_env/re/environment_manager/src/internal/claude/claude_code_executor.go)
adds the startup-context map to the interactive process. The repository's
[Setup hook](../web_setup_hook.sh) can also invoke `web_setup.sh` as a child of Claude.

## From the daemon to a tool

1. The web profile selects [web_env.sh](../../secrets/web_env.sh) as its
   `startup_env_script`. The daemon sources the script in a child shell. It
   captures newly exported and changed variables as an overlay. The script also
   writes the private Attic reader credential to Nix's netrc.
2. On `SessionStart`, the daemon writes the overlay and the profile's
   `env_exports` to `CLAUDE_ENV_FILE` for Bash tool calls. A BuildBuddy key in
   the overlay supplies the private session bazelrc. The parent Claude process
   retains the environment it received at launch.
3. Each background command receives the overlay. Commands marked `after_env`
   also source the generated env file. The web profile runs
   [kubeconfig.py](../../k8s/kubeconfig.py) this way. It decrypts its JWT and
   writes the kubeconfig used by local Kubernetes clients. Anthropic-managed
   cloud MCP servers handle their own credentials.

See the [hook specification](../claude_hook/SPEC.md#common-behaviors) for the
session contract and [env_script.rs](../claude_hook/env_script.rs) for how a
failed startup script or an unchanged variable affects the overlay.

CLI sessions obtain credentials through direnv and [cli_env.sh](../../secrets/cli_env.sh)
before the daemon starts.
