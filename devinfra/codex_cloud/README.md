# Codex Cloud environment bootstrap for `ducktape`

This directory contains a unified Codex Cloud setup/maintenance script for this monorepo,
modeled after the Claude web setup goals:

- bootstrap from monorepo source-of-truth scripts (`devinfra/setup_buildbuddy.sh`)
- optionally install Nix + `.#devtools` for tool parity with local workflows
- support SOPS via `SOPS_AGE_KEY` injected through Codex environment config

## Files

- `setup.sh`: unified script with `--mode=install|maintenance`
- `install.sh`: thin wrapper to `setup.sh --mode=install`
- `maintenance.sh`: thin wrapper to `setup.sh --mode=maintenance`
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

If `SOPS_AGE_KEY` is present during setup, `setup.sh` appends an export to
`~/.bashrc` so Bash tool sessions inherit it.

This is intentional: Codex Cloud docs state setup runs in a separate Bash
session and recommend `~/.bashrc` for persistence into the agent phase.

## What this setup does

1. Detects repo root and logs commit identity.
2. Installs Nix (Determinate installer) if missing.
3. Installs the repo `.#devtools` profile.
4. Persists nix profile sourcing in `~/.bashrc` so agent Bash sessions get Nix tools on `PATH`.
5. Decrypts BuildBuddy API key from SOPS (when possible) and runs `devinfra/setup_buildbuddy.sh`.
6. Configures BuildBuddy remote selection:
   - uses `origin` when it already points directly to GitHub
   - falls back to `github-no-proxy` only when `origin` is proxied/non-GitHub
7. Persists `SOPS_AGE_KEY` into `~/.bashrc` when provided.

Maintenance refreshes git remotes, BuildBuddy config, and devtools profile,
then validates `bb` / `bbr` availability.

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
   - Scripts assume Linux x86_64 and that Codex agent Bash sessions source `~/.bashrc` (as documented).
   - If image changes, update install script accordingly.

## Validation command (inside repo)

```bash
bash -n devinfra/codex_cloud/install.sh
bash -n devinfra/codex_cloud/maintenance.sh
bash -n devinfra/codex_cloud/setup.sh
```
