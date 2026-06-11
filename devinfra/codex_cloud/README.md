# Codex Cloud environment bootstrap for `ducktape`

This directory contains a unified Codex Cloud setup/maintenance script for this monorepo,
modeled after the Claude web setup goals:

- bootstrap from monorepo source-of-truth scripts (`devinfra/setup_buildbuddy.sh`)
- optionally install Nix + `.#devtools` for tool parity with local workflows
- support SOPS via `SOPS_AGE_KEY` injected through Codex environment config

## Files

- `setup.sh`: unified script with `--mode=install|maintenance`
- `codex_cloud_agent_configuration_plan.md`: detailed research/plan doc collected for Cloud behavior, hooks, and rollout strategy

## Codex Cloud environment configuration

Recommended initial config in Codex Cloud:

- **Setup script**: `bash devinfra/codex_cloud/setup.sh --mode=install`
- **Maintenance script**: `bash devinfra/codex_cloud/setup.sh --mode=maintenance`

### Environment variables vs secrets

Codex Cloud behavior (per OpenAI docs):

- Environment variables are available in setup and agent phases.
- Secrets are setup-only and removed before agent execution.

For this repo:

- Put `SOPS_AGE_KEY` in **Environment variables** if the agent itself must run `sops`.
- `setup.sh` decrypts the BuildBuddy key from
  `cluster/k8s/agents/shared-secrets/buildbuddy-api-key.sops.yaml` when
  `SOPS_AGE_KEY` is available.

If `SOPS_AGE_KEY` is present during setup, `setup.sh` appends exports to
`~/.bashrc` and ensures `~/.bash_profile` sources `~/.bashrc`, so both
interactive and login shell sessions inherit it without duplicate exports.

This is intentional: Codex Cloud docs state setup runs in a separate Bash
session and recommend `~/.bashrc` for persistence into the agent phase.

## What this setup does

1. Detects repo root and logs commit identity.
2. Installs Nix (Determinate installer) if missing.
3. Installs the repo `.#devtools` profile.
4. Installs repo pre-commit Git hooks (`pre-commit install --install-hooks`).
5. Persists nix profile sourcing in `~/.bashrc`, prepends Nix profile bins on `PATH` (so Nix-provided tooling like Prettier wins over user-level installs), and ensures `~/.bash_profile` sources `~/.bashrc`.
6. Decrypts BuildBuddy API key from SOPS (when possible) and runs `devinfra/setup_buildbuddy.sh`.
7. Configures BuildBuddy remote selection:
   - uses `origin` when it already points directly to GitHub
   - falls back to `github-no-proxy` when `origin` is proxied/non-GitHub
   - if `origin` is missing, creates `github-no-proxy` and selects it
8. Writes Codex Bazel config files:
   - `~/.config/bazel/bbr.bazelrc` for BuildBuddy metadata (`ROLE`, session tag)
   - `~/.config/bazel/codex.bazelrc` with `try-import` wiring for BuildBuddy + bbr metadata
   - `~/.bazelrc` `try-import` entry for `~/.config/bazel/codex.bazelrc`
9. Ensures local `devel` branch exists and tracks a remote `devel` branch (preferring the selected BuildBuddy remote, with fallback to `origin/devel` or `github-no-proxy/devel`) so `bbr` base-branch detection works.
10. Materializes `~/.kube/config` from `secrets/claude-web-k8s-jwt.yaml` via the Nix-managed Python interpreter (`~/.nix-profile/bin/python3 devinfra/k8s/kubeconfig.py`) so `pyyaml` is present consistently.
11. Persists `SOPS_AGE_KEY`, `BUILDBUDDY_API_KEY` (when available during setup), Bazel env vars (`BBR_BAZELRC`, `SESSION_BAZELRC`), and runtime `web_env.sh` sourcing into shell startup files so agent command shells can use BuildBuddy + generated bazelrc files.

Maintenance refreshes git remotes, BuildBuddy config, devtools profile, and
pre-commit hooks, then validates `bb` / `bbr` availability.

## Known gaps / risks

1. **Hooks in Codex Cloud are not yet guaranteed**
   - OpenAI docs clearly describe setup/maintenance scripts and AGENTS behavior.
   - They do not clearly confirm delegated Cloud execution of `.codex/hooks.json`.
   - Plan as if hooks are unavailable unless validated empirically.

2. **SOPS key handling trade-off**
   - `SOPS_AGE_KEY` in environment variables makes runtime decryption possible but is less restrictive than setup-only secrets.
   - If you do not need runtime decryption, prefer keeping key material setup-only.
   - BuildBuddy setup now depends on being able to decrypt
     `cluster/k8s/agents/shared-secrets/buildbuddy-api-key.sops.yaml`.

3. **Nix bootstrap cost**
   - First-run setup may be slow; cached-container resume mitigates this.
   - If setup exceeds practical time limits, fall back to non-Nix Stage A tooling.

4. **Cloud image assumptions**
   - Scripts assume Linux x86_64 and that Codex agent Bash sessions execute shell startup files.
   - If image changes, update install script accordingly.

## Validation command (inside repo)

```bash
bash -n devinfra/codex_cloud/setup.sh
```

## Passing env vars in Codex command runs

In this Codex execution environment, each command runs in a fresh non-login
shell process. That means:

- `export FOO=bar` in one command does **not** persist to the next command.
- `~/.bashrc` updates are persisted to disk, but are not automatically loaded by
  subsequent command executions.

Practical implication: pass required env vars per command invocation (or via a
wrapper script that sets env and executes the command) rather than relying on a
previous command's `export`.

Examples:

```bash
# One-off per command
SOPS_AGE_KEY="$SOPS_AGE_KEY" bbr test //path/to:target

# Multiple vars, single invocation
BUILDBUDDY_API_KEY="$BUILDBUDDY_API_KEY" \
BBR_BAZELRC="$HOME/.config/bazel/bbr.bazelrc" \
bbr build //devinfra:bbr

# Wrapper script pattern
cat > /tmp/run-with-env.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export BBR_BAZELRC="$HOME/.config/bazel/bbr.bazelrc"
export SESSION_BAZELRC="$HOME/.config/bazel/codex.bazelrc"
exec "$@"
EOF
chmod +x /tmp/run-with-env.sh
/tmp/run-with-env.sh bbr test //devinfra:all
```
