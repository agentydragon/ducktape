#!/bin/bash
# Setup script for Claude Code web sessions.
#
# Installs:
#   1. Nix (Determinate Systems installer, designed for non-interactive/CI use)
#   2. devtools — Rust claude-hook, statusline, bbapi, gh, skills; from flake via attic binary cache
#   3. git remote + bbr config for BuildBuddy remote execution
#   4. skills — symlinked per-skill into ~/.claude/skills/ (preserves Anthropic defaults)
#
# Install modes (DUCKTAPE_WEB_SETUP_MODE env var, or --mode=<...> arg):
#   profile       (default) — `nix profile install .#devtools`, then manually
#                  symlink skills into ~/.claude/skills/ (steps 2 + 4 above).
#   home-manager  — `home-manager switch --flake .#claude-web`. Home Manager
#                  installs the same devtools and deploys Claude Code settings +
#                  skills through the shared HM skills module (nix/home/skills.nix),
#                  so step 4 is owned by HM. See homeConfigurations.claude-web in
#                  flake.nix and <nix/home/hosts/claude-web.nix>.
#
# Secrets are NOT decrypted here. SOPS_AGE_KEY is a user UI env var and is
# only available to Claude Code and its subprocesses — not to this setup script.
# This script is run directly by environment-manager as a bash init script
# (process.ExecuteScript → temp file "init-script-*.sh"), inheriting only the
# container env. `claude --init-only` (which fires Setup+SessionStart hooks) runs
# separately afterward. Neither step receives user UI vars.
# Decryption happens exclusively in the claude-hook daemon via startup_env_script.
#
# IMPORTANT: This script always exits 0 so the session starts even if setup
# fails. Failures are logged to /tmp/web-setup.log.
#
# Set DUCKTAPE_WEB_SETUP_UPLOAD_LOG=1 to upload the log to ix.io on completion
# (useful for debugging sessions where you can't read the log directly).
#
# Usage (Claude Code web UI setup command):
#   bash ducktape/devinfra/claude/web_setup.sh
#
# Historical --impl=<python|rust> arguments are accepted for compatibility, but
# active sessions always install the Rust claude-hook implementation.
#
# CLEANUP(added 2026-04-19): this script runs TWICE per session — once as the
# init script (no user-UI env vars, always installs python default) and
# once via the Setup hook (web_setup_hook.sh, has UI env vars). Once the Setup hook
# is confirmed to fire reliably across all session modes (new / resume
# / resume-cached / setup-only — see devinfra/claude/README.md) and is
# sufficient on its own, make the init-script path a no-op (or drop it
# from the Claude Code web UI "Setup Command" field) and let the Setup
# hook own devtools install. Remove this tombstone after ≥1 week of
# live sessions with no "Setup hook missed firing" fallbacks needed.

LOG_FILE="/tmp/web-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

set -euo pipefail

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*"
}

warn() {
  log "WARNING: $*" >&2
}

# Hook implementation is Rust-only. Keep parsing the old selector so stale web
# UI env vars or setup args do not break session startup.
HOOK_IMPL="rust"
# Install mode: "profile" (nix profile install) or "home-manager".
MODE="${DUCKTAPE_WEB_SETUP_MODE:-profile}"
for arg in "$@"; do
  case "$arg" in
    --impl=*) warn "Ignoring deprecated $arg; claude-hook is Rust-only" ;;
    --mode=*) MODE="${arg#--mode=}" ;;
  esac
done
case "$MODE" in
  profile | home-manager) ;;
  *)
    warn "Unknown DUCKTAPE_WEB_SETUP_MODE='$MODE'; falling back to 'profile'"
    MODE="profile"
    ;;
esac
if [ -n "${DUCKTAPE_CLAUDE_HOOK_IMPL:-}" ] && [ "${DUCKTAPE_CLAUDE_HOOK_IMPL:-}" != "rust" ]; then
  warn "Ignoring deprecated DUCKTAPE_CLAUDE_HOOK_IMPL=$DUCKTAPE_CLAUDE_HOOK_IMPL; claude-hook is Rust-only"
fi
DEVTOOLS_OUTPUT="devtools"

FLAKE="path:$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETUP_COMMIT=$(git -C "${FLAKE#path:}" rev-parse HEAD 2>/dev/null || echo 'unknown')
log "web_setup.sh commit: $SETUP_COMMIT"

# --- Step 1: Install Nix (Determinate Systems installer) ---
# Uses the Determinate Systems installer instead of the official one because:
#   - Designed for CI/non-interactive environments (no TTY assumptions)
#   - Single static binary — no shell script wrapping nix-env -i
#   - Avoids the "reading a line: Input/output error" bug where the official
#     installer's nix-env tries to read from a TTY that doesn't exist
#   - --init none skips systemd/launchd (not available in containers)
# Pinned binary from GitHub releases, SHA256-verified.
log "Installing Nix (Determinate installer)..."
bash "${FLAKE#path:}/devinfra/install_nix_determinate.sh"
# shellcheck disable=SC1091
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
log "Nix installation complete."

# --- Step 2: Install web session tools ---
# Debug: dump environment keys for proxy/cert diagnostics (no values — avoids logging JWTs).
log "--- environment keys ---"
env | cut -d= -f1 | sort
log "---"

log "Connectivity check (cache.nixos.org)..."
curl -fsSL --max-time 10 https://cache.nixos.org/nix-cache-info || warn "cache.nixos.org check failed — continuing anyway"

log "--- nix.conf ---"
cat /etc/nix/nix.conf
log "--- nix show-config ---"
nix show-config 2>/dev/null | grep -E "max-jobs|sandbox|build-users" || true
log "---"

log "Installing web session tools..."
log "  FLAKE=${FLAKE}"
log "  pwd=$(pwd)"
log "  flake.nix exists: $(test -f flake.nix && echo yes || echo no)"
log "  ls pwd:"
ls -la
# Pass --max-jobs explicitly to override any nix.conf misconfiguration.
#
# CRITICAL: `nix profile install` is a no-op when the attribute path is already
# in the profile — it matches by attrpath, not by evaluated store hash. On
# persistent Firecracker rootfs `web_setup.sh` re-runs on every session
# (environment-manager's `Initialize` fires `init_script` unconditionally —
# verified in <devinfra/claude/web_env/re/environment_manager/src/internal/envtype/anthropic/anthropic.go>
# Initialize(), Step 3 at line ~361 is not gated on session mode). Without an
# explicit remove, the installed devtools derivation freezes at whatever pin
# was current on container first-boot, even though `nix/artifact-pins.json` in
# the working tree has moved forward. The downstream symptom is agent sessions
# running a stale devtools profile against fresh profile YAML / hook behavior,
# which crashes SessionStart as soon as the schema churns. Remove
# first so `install` re-evaluates `.#devtools` against the current flake.
#
# See <devinfra/claude/docs/web-setup-debug.md> ("Pin drift on persistent rootfs").
# TODO: `attic login` against cache.allegedly.works/{main,gaffer} before this
# install so devtools (and any gaffer-built closures pulled transitively) hit
# the private cache instead of building from source. The reader JWT is in
# secrets/claude-web-attic.yaml, auto-rotated by the in-cluster
# attic-jwt-rotation CronJob; web_env.sh decrypts SOPS files at hook-daemon
# startup but isn't running yet at this point in init_script. Sketch:
#   if [ -f /tmp/_secret_attic_token ]; then
#     attic login allegedly https://cache.allegedly.works \
#       "$(cat /tmp/_secret_attic_token)"
#   fi
# Plumbing needed: have web_env.sh write the decrypted token to a known
# path (mode 0600, owned by user) before init_script runs, OR fold the
# sops decrypt into web_setup.sh directly using SOPS_AGE_KEY. Today the
# install path falls back to building from source if main isn't reachable
# anonymously — slow but works.
log "Install mode: $MODE"
if [ "$MODE" = "home-manager" ]; then
  # Home Manager installs the same devtools (homeConfigurations.claude-web reuses
  # the flake's devToolPackages list) and deploys skills + Claude Code settings
  # itself, so the standalone skill symlink in step 4 is skipped below.
  #
  # --impure: claude-web reads home.username/homeDirectory from $USER/$HOME.
  # -b hm-backup: back up any pre-existing dotfiles (Anthropic-landed
  #   ~/.claude/settings.json etc.) instead of failing the activation.
  # nix run .#home-manager pins the HM CLI to our flake input (release-25.11).
  nix run --max-jobs auto "${FLAKE}#home-manager" -- switch \
    --impure -b hm-backup --max-jobs auto --flake "${FLAKE}#claude-web"
  log "Home Manager profile 'claude-web' activated."
  # home.packages land in the Nix user profile. Determinate/XDG-base-dir Nix may
  # site it under ~/.local/state instead of ~/.nix-profile; pick whichever has bin/.
  NIX_PROFILE="${HOME}/.nix-profile"
  if [ ! -d "${NIX_PROFILE}/bin" ] && [ -d "${HOME}/.local/state/nix/profiles/profile/bin" ]; then
    NIX_PROFILE="${HOME}/.local/state/nix/profiles/profile"
  fi
else
  # CRITICAL: remove first so install re-evaluates against the current flake
  # (see the "Pin drift on persistent rootfs" comment in the CRITICAL note above).
  nix profile remove devtools 2>/dev/null || true
  nix profile remove devtools-rust 2>/dev/null || true
  NIX_PROFILE="/nix/var/nix/profiles/default"
  nix profile install --max-jobs auto "${FLAKE}#${DEVTOOLS_OUTPUT}"
  log "Dev tools installed (impl=$HOOK_IMPL)."
fi

# Symlink all Nix-installed binaries into /usr/local/bin so they're on PATH.
# Claude Code is launched directly (not via login shell), so the Nix profile bin
# is not in PATH when hooks run. /usr/local/bin is always in PATH.
for bin in "${NIX_PROFILE}"/bin/*; do
  ln -sfn "$bin" /usr/local/bin/"$(basename "$bin")"
done

PROJECT_DIR="${FLAKE#path:}"

# --- Step 3: Add github-no-proxy remote for bbr ---
# Claude Code web sessions use a local git proxy as 'origin'
# (http://127.0.0.1:<port>/git/...). When 'bb remote' sends a RunRequest to
# the BuildBuddy cloud runner, it embeds the selected remote's URL in
# RepoState.repo.url. The runner then fetches from that URL. The local proxy
# URL is unreachable from the cloud runner, so bbr fails.
#
# Fix: add a 'github-no-proxy' remote that points directly at GitHub.
#
# bb source: https://github.com/buildbuddy-io/buildbuddy (cli/remotebazel/)
#
# bb auto-selects a single remote without prompting. With 2+ remotes, in TTY
# mode it shows a selection TUI; in non-TTY mode (pre-commit hooks, CI) it
# reads buildbuddy.remote-bazel-remote-name and uses the named remote directly.
# Value must be a remote NAME (not a URL) — bb resolves the URL from git config.
GITHUB_REMOTE_URL="https://github.com/agentydragon/ducktape"
if git -C "$PROJECT_DIR" remote get-url github-no-proxy &>/dev/null; then
  log "git remote 'github-no-proxy' already exists, skipping."
else
  git -C "$PROJECT_DIR" remote add github-no-proxy "$GITHUB_REMOTE_URL"
  log "Added git remote 'github-no-proxy' -> $GITHUB_REMOTE_URL"
fi
git -C "$PROJECT_DIR" config buildbuddy.remote-bazel-remote-name github-no-proxy
log "Set buildbuddy.remote-bazel-remote-name=github-no-proxy"

# --- Step 4: Symlink skills into ~/.claude/skills/ ---
# Per-skill symlinks instead of replacing the directory, so Anthropic's
# pre-landed default skills are preserved.
# Skipped in home-manager mode: the claude-web profile deploys skills via the
# shared HM skills module (nix/home/skills.nix).
if [ "$MODE" = "home-manager" ]; then
  log "Skills deployed by Home Manager (claude-web profile); skipping manual symlink."
else
  log "Deploying skills to ~/.claude/skills/..."
  mkdir -p ~/.claude/skills
  for skill in "${NIX_PROFILE}"/share/claude-hooks/skills/*/; do
    ln -sfn "$skill" ~/.claude/skills/"$(basename "$skill")"
  done
fi

log "Setup complete. Log: ${LOG_FILE}"

if [ "${DUCKTAPE_WEB_SETUP_UPLOAD_LOG:-0}" = "1" ]; then
  UPLOAD_URL=$(curl -s -F "f:1=@${LOG_FILE}" ix.io 2>/dev/null || echo "upload failed")
  log "Log uploaded: ${UPLOAD_URL}"
fi
