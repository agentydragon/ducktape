#!/bin/bash
# Setup script for Claude Code web sessions.
#
# Installs:
#   1. Nix (Determinate Systems installer, designed for non-interactive/CI use)
#   2. devtools — claude-hooks, bbapi, gh, skills; from flake via attic binary cache
#   3. skills — symlinked per-skill into ~/.claude/skills/ (preserves Anthropic defaults)
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
# Debug: dump environment for proxy/cert diagnostics.
echo "--- environment ---"
env | sed 's/^\(DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN=\).*/\1<redacted>/' | sort
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

# --- Step 3: Set profile for web mode ---
# Write to .claude/settings.local.json so Claude Code injects the env var into
# hook processes — the export below only covers the current shell process.
SETTINGS_LOCAL="${FLAKE#path:}/.claude/settings.local.json"
echo '{"env":{"DUCKTAPE_CLAUDE_HOOKS_PROFILE":".claude_hooks/web.yaml"}}' > "$SETTINGS_LOCAL"
echo "[$(date -Iseconds)] Wrote DUCKTAPE_CLAUDE_HOOKS_PROFILE to ${SETTINGS_LOCAL}"

# --- Step 4: Symlink skills into ~/.claude/skills/ ---
# Per-skill symlinks instead of replacing the directory, so Anthropic's
# pre-landed default skills are preserved.
echo "Deploying skills to ~/.claude/skills/..."
mkdir -p ~/.claude/skills
for skill in "${NIX_PROFILE}"/share/claude-hooks/skills/*/; do
  ln -sfn "$skill" ~/.claude/skills/"$(basename "$skill")"
done

echo "[$(date -Iseconds)] Setup complete. Log: ${LOG_FILE}"
