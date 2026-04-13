<%import os%>\
## Secrets
% if setup.buildbuddy_api_key:
`BUILDBUDDY_API_KEY` loaded (from `devinfra/secrets/cli_env.sh`).
% else:
`BUILDBUDDY_API_KEY` not loaded — Bazel RBE unavailable. Check `devinfra/secrets/cli_env.sh`.
% endif
% if setup.github_token:
`GITHUB_TOKEN` available (personal PAT from home-manager). `gh` CLI and authenticated git operations work.
% endif
`KUBECONFIG` comes from personal config (home-manager `~/.kube/config`), not from the hook daemon. It may or may not be present depending on the user's setup.

## direnv

The session env file runs `direnv export bash` before every Bash tool call. Environment
variables from `.envrc` files are automatically available. When you `cd` to a different
directory, the next command picks up that directory's `.envrc` because Claude Code sources
the session env file after changing to the tracked working directory.
