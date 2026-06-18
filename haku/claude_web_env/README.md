# Haku — Claude Code web "home"

Configuration for the Claude Code web environment that runs Haku. Haku lives
here (on Anthropic infra) and drives the cluster over `kubectl`; the
`haku-sandbox` namespace is its in-cluster compute surface.

## Environment settings (when creating the web environment)

- **Setup script:** `bash ducktape/haku/claude_web_env/setup.sh` — the setup
  command runs from the parent of the repo checkout, so it needs the `ducktape/`
  prefix (same as the shared `bash ducktape/devinfra/claude/web_setup.sh`).
- **Environment variables:**
  - `DUCKTAPE_CLAUDE_HOOKS_PROFILE=haku/claude_web_env/profile.yaml`
  - `SOPS_AGE_KEY=<the haku age key>` — decrypt it from
    `secrets/haku-age-key.sops.yaml` (readable with your user ssh key) and paste it.
- **Prompt:** `Execute haku/claude_web_env/run.md`
- **"Enable common package managers":** on.
- **Allowed domains:** the [default allowed domains](https://code.claude.com/docs/en/claude-code-on-the-web#default-allowed-domains)
  plus:
  - `*.anthropic.com`
  - `*.allegedly.works` — the cluster (kube API, Forgejo, LiteLLM, …)
  - `*.googleapis.com` — Gmail/Calendar read-only REST
  - `*.buildbuddy.io` — RBE/remote cache (only if Haku runs `bbr`)

## Files

- `setup.sh` — environment setup script; delegates to `devinfra/claude/web_setup.sh`.
- `profile.yaml` — the claude-hook profile. Sets the `K8S_*` overrides (→ group
  `haku` / `haku-sandbox`) and runs `bootstrap.sh` as its background command.
- `bootstrap.sh` — profile background command: materializes `~/.kube/config`
  from the haku JWT, writes `~/.netrc` from the `haku-state-git-write` secret,
  and clones `haku-state` into `~/haku-state`.
- `run.md` — the web entrypoint: bootstrap recap + concrete paths, then defers
  to the environment-neutral `haku/run.md` for the run procedure.

## How a session boots

1. `setup.sh` (env creation) → shared web setup: devtools, claude-hook daemon,
   certs, git remotes.
2. `profile.yaml` runs `bootstrap.sh` (each session start): it materializes
   `~/.kube/config` from the SOPS-encrypted haku JWT, then — with cluster access
   in hand — writes `~/.netrc` and clones `haku-state` into `~/haku-state`.
3. The `Execute haku/claude_web_env/run.md` prompt runs a scan and pushes state.

Depends on `secrets/haku-k8s-jwt.yaml` existing (minted by the
`authentik-jwt-rotation` CronJob) — that JWT is what the kubeconfig is built from.
