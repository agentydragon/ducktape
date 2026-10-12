# Haku — Claude Code web "home"

Configuration for the Claude Code web environment that runs Haku. Haku lives
here (on Anthropic infra) and drives the cluster over `kubectl`; the
`haku-sandbox` namespace is its in-cluster compute surface.

## Environment settings (when creating the web environment)

- **Setup script:** paste the contents of [`env_setup.sh`](env_setup.sh) into the
  environment's setup-script dialog (see [Setup script](#setup-script)).
- **Environment variables:**
  - `DUCKTAPE_CLAUDE_HOOKS_PROFILE=haku/runtime/claude_web_env/profile.yaml`
  - `SOPS_AGE_KEY=<the haku age key>` — decrypt it from
    `secrets/haku-age-key.sops.yaml` (readable with your user ssh key) and paste it.
  - **The Claude Code native-telemetry block** from
    <../../../devinfra/claude/README.md> § Web Setup. The `otel forwarder` background
    command below only starts the localhost relay — nothing emits into it unless these
    are set on the environment too. `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative`
    is the load-bearing one: without it Claude Code emits delta-temporality metrics,
    which Alloy's Prometheus exporter drops silently, and Haku's traces reach Tempo
    while its metrics never reach Mimir. See
    <../../../cluster/docs/lessons_learned/2026_07_31_claude_code_otel_delta_temporality.md>.
- **Prompt:** `Execute haku/runtime/claude_web_env/run.md`
- **"Enable common package managers":** on.
- **Allowed domains:** the [default allowed domains](https://code.claude.com/docs/en/claude-code-on-the-web#default-allowed-domains)
  plus:
  - `*.anthropic.com`
  - `*.allegedly.works` — the cluster (kube API, Forgejo, LiteLLM, haku-console, …)
  - `*.buildbuddy.io` — RBE/remote cache (only if Haku runs `bbr`)
  - `nixos.org`, `cache.nixos.org` — Nix channels + binary cache, so the
    `.#agent-haku` install (and any Nix/pre-commit work) pulls from the cache
    instead of failing (observed: `nixos.org` 403 without this).

## Setup script

The environment setup script runs before any repo is cloned, so
[`env_setup.sh`](env_setup.sh) is self-contained: it reads no repo path and clones
nothing. It inlines `devinfra/claude/web_setup.sh` (profile mode),
`devinfra/install_nix_determinate.sh`, `nix/attic-pubkeys.json` and
`devinfra/claude/heal_ccr_bazel_trust.sh`; keep it in sync when those change. It
installs `.#agent-haku` from the flake
`git+https://github.com/agentydragon/ducktape?ref=devel&shallow=1` (override with
`DUCKTAPE_FLAKE_REF`); a plain git fetch avoids the GitHub API rate limit a `github:`
flake ref hits.

What it cannot do without a checkout:

- `reconcile_bbr_remote` (git remote and default-branch setup for `bbr`) is skipped;
  the Setup hook (`web_setup_hook.sh` → `devinfra/claude/web_setup.sh`, run from the
  checkout) does it.

## Files

- `env_setup.sh` — environment setup script, pasted into the setup dialog.
- `profile.yaml` — the claude-hook profile. Sets the `K8S_*` overrides (→ group
  `haku` / `haku-sandbox`) and runs `bootstrap.sh` as its background command.
- `bootstrap.sh` — profile background command: materializes `~/.kube/config`
  from the haku JWT, writes `~/.netrc` from the `haku-forgejo-git` secret,
  writes `~/.config/tea/config.yml` from `haku-forgejo-tea` when the rotator has
  published it, and clones `haku-state` into `~/haku-state`.
- `tea` is present in the installed closure and logs in from the
  forgejo-token-rotation output (`haku-sandbox/haku-forgejo-tea`). Smoke test:
  `tea whoami`.
- `run.md` — the web entrypoint: bootstrap recap + concrete paths, then defers
  to the run procedure in Haku's own state, `memory/procedures/run.md`.

## How a session boots

1. The pasted setup script (env creation) → shared web setup installing `.#agent-haku`
   (`.#devtools` plus fastmcp, himalaya, and tea), claude-hook daemon, certs, git
   remotes.
2. `profile.yaml` runs `bootstrap.sh` (each session start): it materializes
   `~/.kube/config` from the SOPS-encrypted haku JWT, then — with cluster access
   in hand — writes `~/.netrc`, writes tea's config from `haku-forgejo-tea` if
   present, and clones `haku-state` into `~/haku-state`.
3. The `Execute haku/runtime/claude_web_env/run.md` prompt runs a scan and pushes state.

Depends on `secrets/haku-k8s-jwt.yaml` existing (minted by the
`authentik-jwt-rotation` CronJob) — that JWT is what the kubeconfig is built from.

## Gotcha: the Setup hook can clobber `.#agent-haku` tools

`web_setup.sh` runs **twice** per fresh session: once as the init script (via
the pasted setup script, which sets `DUCKTAPE_WEB_SETUP_OUTPUT=agent-haku` → installs Haku's
extra tools)
and once as the **Setup hook** (`web_setup_hook.sh` → `web_setup.sh`), which does
**not** carry that env var. The Setup-hook run was defaulting to `.#devtools` and
running `nix profile remove agent-haku`, **wiping fastmcp** and leaving a dangling
`/usr/local/bin/fastmcp` — so Haku lost its console MCP tools.
On `resume-cached` sessions only the Setup hook runs, so agent-haku never installed
at all.

Fix (in `devinfra/claude/web_setup.sh`): when `DUCKTAPE_WEB_SETUP_OUTPUT` is unset
but `DUCKTAPE_CLAUDE_HOOKS_PROFILE` points at a Haku profile, it now defaults to
`agent-haku` — so **both** paths install the Haku-only tools consistently — and it prunes
dangling `/usr/local/bin` symlinks before re-bridging. Setting
`DUCKTAPE_WEB_SETUP_OUTPUT=agent-haku` as a web-UI env var is an equally valid
explicit override.
