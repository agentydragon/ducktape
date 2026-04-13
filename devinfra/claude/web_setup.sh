#!/bin/bash
# Setup script for Claude Code web sessions.
#
# Installs:
#   1. Nix (Determinate Systems installer, designed for non-interactive/CI use)
#   2. devtools — claude-hooks, bbapi, gh, skills; from flake via attic binary cache
#   3. secrets + profile — decrypt SOPS secrets into .claude/settings.local.json
#   4. skills — symlinked per-skill into ~/.claude/skills/ (preserves Anthropic defaults)
#
# IMPORTANT: This script always exits 0 so the session starts even if setup
# fails. Failures are logged to /tmp/web-setup.log and uploaded to ix.io.
#
# Usage (Claude Code web UI setup command):
#   bash ducktape/devinfra/claude/web_setup.sh

LOG_FILE="/tmp/web-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

set -euo pipefail

FLAKE="path:$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETUP_COMMIT=$(git -C "${FLAKE#path:}" rev-parse HEAD 2>/dev/null || echo 'unknown')
echo "[$(date -Iseconds)] web_setup.sh commit: $SETUP_COMMIT"

# --- Step 1: Install Nix (Determinate Systems installer) ---
# Uses the Determinate Systems installer instead of the official one because:
#   - Designed for CI/non-interactive environments (no TTY assumptions)
#   - Single static binary — no shell script wrapping nix-env -i
#   - Avoids the "reading a line: Input/output error" bug where the official
#     installer's nix-env tries to read from a TTY that doesn't exist
#   - --init none skips systemd/launchd (not available in containers)
# Pinned binary from GitHub releases, SHA256-verified.
echo "[$(date -Iseconds)] Installing Nix (Determinate installer)..."
NIX_INSTALLER_VERSION="v3.17.3"
NIX_INSTALLER_URL="https://github.com/DeterminateSystems/nix-installer/releases/download/${NIX_INSTALLER_VERSION}/nix-installer-x86_64-linux"
NIX_INSTALLER_SHA256="4a84424a0a598b671de21fca1602ea3e74af214d823020afe7aac0056dc032ac"
NIX_INSTALLER_BIN="/tmp/nix-installer"
curl -fsSL "$NIX_INSTALLER_URL" -o "$NIX_INSTALLER_BIN"
echo "${NIX_INSTALLER_SHA256}  ${NIX_INSTALLER_BIN}" | sha256sum -c
chmod +x "$NIX_INSTALLER_BIN"
# Ensure $USER is set — nix profile sourcing is a no-op when $USER is empty.
_saved_user="${USER:-}"
export USER="${USER:-$(id -u -n)}"
# --no-confirm: fully non-interactive
# --init none: no systemd/launchd (container environment)
# --extra-conf: gVisor needs sandbox=false (no kernel namespaces/cgroups),
#   max-jobs=auto for local symlinkJoin/buildEnv builds not in cache.
# CLEANUP(2026-03-27): cache.allegedly.works is currently down (503). Falling back to
#   cache.nixos.org only. Restore once the cache is back:
#     --extra-conf "substituters = https://cache.allegedly.works/main https://cache.nixos.org"
#     --extra-conf "trusted-public-keys = cache.allegedly.works-1:OX/cis8G1W13DALkGvhdUZ1OY3yGATbXw8+tIc8J7oA= cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
"$NIX_INSTALLER_BIN" install linux \
  --no-confirm \
  --init none \
  --extra-conf "sandbox = false" \
  --extra-conf "max-jobs = auto" \
  --extra-conf "system-features =" \
  --extra-conf "substituters = https://cache.nixos.org" \
  --extra-conf "trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
# shellcheck disable=SC1091
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
# Restore $USER to its original state so we don't leak a side-effect.
if [ -z "$_saved_user" ]; then unset USER; else USER="$_saved_user"; fi
unset _saved_user
echo "[$(date -Iseconds)] Nix installation complete."

# --- Step 2: Install web session tools ---
# Debug: dump environment keys for proxy/cert diagnostics (no values — avoids logging JWTs).
echo "--- environment keys ---"
env | cut -d= -f1 | sort
echo "---"

echo "Connectivity check (cache.nixos.org)..."
curl -fsSL --max-time 10 https://cache.nixos.org/nix-cache-info

echo "--- nix.conf ---"
cat /etc/nix/nix.conf
echo "--- nix show-config ---"
nix show-config 2>/dev/null | grep -E "max-jobs|sandbox|build-users" || true
echo "---"

echo "[$(date -Iseconds)] Installing web session tools..."
echo "  FLAKE=${FLAKE}"
echo "  pwd=$(pwd)"
echo "  flake.nix exists: $(test -f flake.nix && echo yes || echo no)"
echo "  ls pwd:"
ls -la
# Pass --max-jobs explicitly to override any nix.conf misconfiguration.
nix profile install --max-jobs auto "${FLAKE}#devtools"
echo "[$(date -Iseconds)] Dev tools installed."

# Symlink all Nix-installed binaries into /usr/local/bin so they're on PATH.
# Claude Code is launched directly (not via login shell), so the Nix profile bin
# is not in PATH when hooks run. /usr/local/bin is always in PATH.
NIX_PROFILE="/nix/var/nix/profiles/default"
for bin in "${NIX_PROFILE}"/bin/*; do
  ln -sfn "$bin" /usr/local/bin/"$(basename "$bin")"
done

# --- Step 3: Decrypt secrets and write settings.local.json ---
# sops is now on PATH (installed by devtools in Step 2).
# These secrets are used by Claude Code itself and any non-kube MCP servers.
# The kube MCP server (claude-sandbox-kubectl) self-decrypts via kube_from_sops.sh
# and does not read from settings.local.json. See docs/secrets_env_flow.md.
#
# The hook daemon also decrypts independently via startup_env_script at daemon
# startup — the reliable path for hook subprocesses.
PROJECT_DIR="${FLAKE#path:}"
eval "$("$PROJECT_DIR/devinfra/secrets/web_env.sh" 2>/tmp/web-env-stderr.log)" || true
cat /tmp/web-env-stderr.log >&2

# Python writes the JSON — safe quoting, no shell escaping footguns.
# Note: DUCKTAPE_CLAUDE_HOOKS_PROFILE is not written here; configure it as an
# env var in the Claude Code web UI (along with the SOPS_AGE_KEY age private key).
SETTINGS_LOCAL="$PROJECT_DIR/.claude/settings.local.json"
SETTINGS_LOCAL="$SETTINGS_LOCAL" python3 <<'PYEOF'
import json, os, sys
from pathlib import Path

keys = ["BUILDBUDDY_API_KEY", "DUCKTAPE_OTEL_BEARER_TOKEN",
        "GITHUB_TOKEN", "K8S_TOKEN", "DUCKTAPE_CI_READ_GITHUB_TOKEN"]
present = {k: v for k in keys if (v := os.environ.get(k))}
missing = [k for k in keys if k not in present]
if missing:
    print(f"NOTE: secrets not yet available for settings.local.json: {missing}", file=sys.stderr)
    print("      Hook daemon will decrypt via startup_env_script at session start.", file=sys.stderr)
Path(os.environ["SETTINGS_LOCAL"]).write_text(json.dumps({"env": present}))
PYEOF
echo "[$(date -Iseconds)] Wrote secrets to ${SETTINGS_LOCAL}"

# --- Step 4: Add github-no-proxy remote for bbr ---
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
  echo "[$(date -Iseconds)] git remote 'github-no-proxy' already exists, skipping."
else
  git -C "$PROJECT_DIR" remote add github-no-proxy "$GITHUB_REMOTE_URL"
  echo "[$(date -Iseconds)] Added git remote 'github-no-proxy' -> $GITHUB_REMOTE_URL"
fi
git -C "$PROJECT_DIR" config buildbuddy.remote-bazel-remote-name github-no-proxy
echo "[$(date -Iseconds)] Set buildbuddy.remote-bazel-remote-name=github-no-proxy"

# --- Step 5: Symlink skills into ~/.claude/skills/ ---
# Per-skill symlinks instead of replacing the directory, so Anthropic's
# pre-landed default skills are preserved.
echo "Deploying skills to ~/.claude/skills/..."
mkdir -p ~/.claude/skills
for skill in "${NIX_PROFILE}"/share/claude-hooks/skills/*/; do
  ln -sfn "$skill" ~/.claude/skills/"$(basename "$skill")"
done

echo "[$(date -Iseconds)] Setup complete. Log: ${LOG_FILE}"
