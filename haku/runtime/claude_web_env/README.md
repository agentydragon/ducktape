# Haku — Claude Code web "home"

Configuration for the Claude Code web environment that runs Haku. Haku lives
here (on Anthropic infra) and drives the cluster over `kubectl`; the
`haku-sandbox` namespace is its in-cluster compute surface.

## Environment settings (when creating the web environment)

- **Setup script:** paste the script from [Setup script](#setup-script) below
  directly into the environment's setup-script dialog. The setup script runs
  _before_ any repo is cloned, so it cannot read any file in this repo or clone
  it. It is a self-contained, inlined copy of `devinfra/claude/web_setup.sh` and
  its helpers that installs `.#agent-haku` from the remote flake
  `github:agentydragon/ducktape/devel` (override with `DUCKTAPE_FLAKE_REF`).
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

Paste verbatim into the environment setup dialog. It inlines
`devinfra/claude/web_setup.sh` (profile mode),
`devinfra/install_nix_determinate.sh`, `nix/attic-pubkeys.json` and
`devinfra/claude/heal_ccr_bazel_trust.sh`; keep it in sync when those change.

What cannot be done checkout-free:

- `reconcile_bbr_remote` (git remote and default-branch setup for `bbr`) needs the
  git checkout, so it is skipped here. The Setup hook (`web_setup_hook.sh` →
  `devinfra/claude/web_setup.sh`, run from the checkout) does it later.
- The flake is fetched from GitHub by Nix (`github:` → codeload tarball), so
  `api.github.com`/`codeload.github.com` must be reachable at setup time.

```bash
#!/bin/bash
# Haku Claude Code web environment setup script — paste verbatim into the
# environment's "Setup script" dialog.
#
# Self-contained: this runs BEFORE any repo is cloned, so it reads no ducktape
# path and clones nothing. It is an inlined copy of devinfra/claude/web_setup.sh
# (profile mode) plus devinfra/install_nix_determinate.sh,
# nix/attic-pubkeys.json, devinfra/claude/reconcile_bbr_remote.sh and
# devinfra/claude/heal_ccr_bazel_trust.sh. The flake is referenced remotely
# (DUCKTAPE_FLAKE_REF, default github:agentydragon/ducktape/devel) instead of a
# local path: checkout. Keep in sync with those files.
LOG_FILE="/tmp/web-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1
set -euo pipefail
log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }
warn() { log "WARNING: $*" >&2; }

WEB_SETUP_OUTPUT="${DUCKTAPE_WEB_SETUP_OUTPUT:-agent-haku}"
FLAKE="${DUCKTAPE_FLAKE_REF:-github:agentydragon/ducktape/devel}"
log "env setup (inlined web_setup.sh): FLAKE=$FLAKE OUTPUT=$WEB_SETUP_OUTPUT"

# --- Step 1: Install Nix (Determinate installer; inlined install_nix_determinate.sh) ---
if [ -x /nix/var/nix/profiles/default/bin/nix ] && /nix/var/nix/profiles/default/bin/nix --version >/dev/null 2>&1; then
  log "Nix already installed ($(/nix/var/nix/profiles/default/bin/nix --version)); skipping installer."
else
  log "Installing Nix (Determinate installer)..."
  NIX_INSTALLER_VERSION="v3.17.3"
  NIX_INSTALLER_SHA256="4a84424a0a598b671de21fca1602ea3e74af214d823020afe7aac0056dc032ac"
  NIX_INSTALLER_BIN="$(mktemp /tmp/nix-installer.XXXXXX)"
  curl -fsSL "https://github.com/DeterminateSystems/nix-installer/releases/download/${NIX_INSTALLER_VERSION}/nix-installer-x86_64-linux" -o "$NIX_INSTALLER_BIN"
  echo "${NIX_INSTALLER_SHA256}  ${NIX_INSTALLER_BIN}" | sha256sum -c
  chmod +x "$NIX_INSTALLER_BIN"
  USER="${USER:-$(id -u -n)}" "$NIX_INSTALLER_BIN" install linux \
    --no-confirm --init none \
    --extra-conf "sandbox = false" \
    --extra-conf "max-jobs = auto" \
    --extra-conf "system-features =" \
    --extra-conf "substituters = https://cache.nixos.org" \
    --extra-conf "trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
  rm -f "$NIX_INSTALLER_BIN"
fi
unset __ETC_PROFILE_NIX_SOURCED
# shellcheck disable=SC1091
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
log "Nix installation complete."

curl -fsSL --max-time 10 https://cache.nixos.org/nix-cache-info || warn "cache.nixos.org check failed — continuing anyway"

# --- Attic substituter (cache.allegedly.works); pubkeys inlined from nix/attic-pubkeys.json ---
mkdir -p /nix/var/determinate
[ -f /nix/var/determinate/netrc ] || (umask 077 && : >/nix/var/determinate/netrc)
if ! grep -q 'cache\.allegedly\.works' /etc/nix/nix.custom.conf 2>/dev/null; then
  cat >>/etc/nix/nix.custom.conf <<'EOF'

# Attic substituter (added by env setup script; reader JWT lands in the netrc via web_env.sh)
netrc-file = /nix/var/determinate/netrc
extra-substituters = https://cache.allegedly.works/public https://cache.allegedly.works/main
extra-trusted-public-keys = main:W4FKePyV8sQL66PpOnh54fAbuuSPKpgXPS4mHAp77Zg= gaffer:JJZtWNWK6mSoKoBkBTI4997S2/5nBj2NC4rLt1I7TDQ= public:BIhpTQ8uN+noKrwtlNBgSXBouH+NY0XycBXKbWdk0z0=
fallback = true
EOF
  log "Attic substituter configured."
fi

# --- Step 2: Install tools from the remote flake ---
nix profile remove devtools 2>/dev/null || true
nix profile remove agent-haku 2>/dev/null || true
NIX_PROFILE="/nix/var/nix/profiles/default"
nix --extra-experimental-features 'nix-command flakes' profile install --refresh --max-jobs auto "${FLAKE}#${WEB_SETUP_OUTPUT}"
log "Dev tools installed (${FLAKE}#${WEB_SETUP_OUTPUT})."
for link in /usr/local/bin/*; do
  [ -L "$link" ] && [ ! -e "$link" ] && rm -f "$link"
done
for bin in "${NIX_PROFILE}"/bin/*; do
  ln -sfn "$bin" /usr/local/bin/"$(basename "$bin")"
done

# --- Step 3: bbr remote reconciliation — SKIPPED ---
# reconcile_bbr_remote needs the ducktape git checkout, which does not exist
# yet. The Setup hook (web_setup_hook.sh -> devinfra/claude/web_setup.sh, run
# from the checkout) performs it later.
log "Skipping bbr remote reconciliation (no checkout at env-setup time; Setup hook does it)."

# --- Step 3b: Heal CCR Java truststore for Bazel (inlined heal_ccr_bazel_trust.sh) ---
if [ ! -e /etc/bazel.bazelrc ] && [ -e /root/.ccr/ca-bundle.crt ] && [ -r /etc/ssl/certs/java/cacerts ]; then
  cat >/etc/bazel.bazelrc <<'EOF'
# Managed by the Haku env setup script (copy of devinfra/claude/heal_ccr_bazel_trust.sh).
# See devinfra/claude/docs/ccr_bazel_truststore_race.md.
startup --host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit
EOF
  log "wrote /etc/bazel.bazelrc (CCR truststore-seed heal)."
fi

# --- Step 4: Symlink skills into ~/.claude/skills/ ---
mkdir -p ~/.claude/skills
for skill in "${NIX_PROFILE}"/share/claude-hooks/skills/*/; do
  [ -d "$skill" ] && ln -sfn "$skill" ~/.claude/skills/"$(basename "$skill")"
done

# --- Step 5: User-level Bazel disk cache ---
BAZEL_DISK_CACHE="${HOME}/.cache/bazel/disk"
mkdir -p "$BAZEL_DISK_CACHE"
HOME_BAZELRC="${HOME}/.bazelrc"
BEGIN="# >>> ducktape web disk cache >>>"
END="# <<< ducktape web disk cache <<<"
[ -f "$HOME_BAZELRC" ] && sed -i "/$BEGIN/,/$END/d" "$HOME_BAZELRC"
cat >>"$HOME_BAZELRC" <<EOF
$BEGIN
# Managed by devinfra/claude/web_setup.sh — do not edit by hand.
build --disk_cache=${BAZEL_DISK_CACHE}
build --experimental_disk_cache_gc_max_size=50G
build --experimental_disk_cache_gc_max_age=7d
$END
EOF
log "Setup complete. Log: ${LOG_FILE}"
```

## Files

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
