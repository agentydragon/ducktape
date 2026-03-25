#!/bin/bash
# Setup script for Claude Code web sessions.
#
# Installs:
#   1. Nix (official single-user installer with permissive config)
#   2. web-session — claude-hooks, bbapi, gh, skills; from flake via attic binary cache
#   3. skills — symlinked per-skill into ~/.claude/skills/ (preserves Anthropic defaults)
#
# IMPORTANT: This script always exits 0 so the session starts even if setup
# fails. Failures are logged to /tmp/web-setup.log and uploaded to ix.io.
#
# Usage (Claude Code web UI setup command):
#   curl -fsSL https://raw.githubusercontent.com/agentydragon/ducktape/devel/devinfra/claude/web_setup.sh | bash

LOG_FILE="/tmp/web-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

# Always exit 0 — upload log on failure so we can debug from inside the session.
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    echo ""
    echo "=== SETUP FAILED (exit $rc) ==="
    echo "Log saved to: $LOG_FILE"
    # Upload full log to ix.io for debugging (UI truncates output).
    local url
    if url=$(curl -fsSL -F 'f:1=@'"$LOG_FILE" ix.io 2>/dev/null); then
      echo "Full log: $url"
    else
      echo "(ix.io upload failed)"
    fi
  fi
  exit 0
}
trap on_exit EXIT

set -euo pipefail

FLAKE="github:agentydragon/ducktape"

# --- Step 1: Install Nix ---
# Write nix.conf BEFORE the installer runs. The installer internally runs
# `nix-env -i` which reads this config. Without it:
#   - build-users-group defaults to 'nixbld' (group doesn't exist in container)
#   - the install fails immediately
# We allow local builds here because `nix-env -i` builds a trivial
# user-environment.drv (just profile symlinks). Step 2 locks this down.
echo "Pre-configuring Nix for installation..."
mkdir -p ~/.config/nix
cat >~/.config/nix/nix.conf <<'EOF'
build-users-group =
sandbox = false
EOF

echo "Installing Nix..."
# Ensure $USER is set — the installer's nix.sh (sourced below) is a no-op
# when $USER is empty, which means PATH never gets ~/.nix-profile/bin.
# The container runs as root but may not have $USER in the environment.
_saved_user="${USER:-}"
export USER="${USER:-$(id -u -n)}"
curl -fsSL https://nixos.org/nix/install | sh -s -- --no-daemon
# shellcheck disable=SC1091
. ~/.nix-profile/etc/profile.d/nix.sh
# Restore $USER to its original state so we don't leak a side-effect.
if [ -z "$_saved_user" ]; then unset USER; else USER="$_saved_user"; fi
unset _saved_user

# --- Step 2: Configure Nix for gVisor ---
# sandbox=false: gVisor already provides isolation; Nix's own sandbox needs
#   kernel features (namespaces, cgroups) that gVisor doesn't fully support.
# max-jobs=auto: local builds work fine on gVisor with sandbox=false.
#   Needed for symlinkJoin/buildEnv derivations that won't be in the cache.
echo "Configuring Nix for gVisor..."
cat >~/.config/nix/nix.conf <<'EOF'
build-users-group =
experimental-features = nix-command flakes
sandbox = false
max-jobs = auto
system-features =
substituters = https://cache.allegedly.works/main https://cache.nixos.org
trusted-public-keys = cache.allegedly.works-1:OX/cis8G1W13DALkGvhdUZ1OY3yGATbXw8+tIc8J7oA= cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY=
EOF

# --- Step 3: Install web session tools ---
# Debug: dump environment for proxy/cert diagnostics.
echo "--- environment ---"
env | sed 's/^\(DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN=\).*/\1<redacted>/' | sort
echo "---"

echo "Connectivity check (cache.allegedly.works)..."
curl -fsSL --max-time 10 https://cache.allegedly.works/main/nix-cache-info

echo "Installing web session tools..."
nix profile install "${FLAKE}#web-session"

# --- Step 4: Symlink skills into ~/.claude/skills/ ---
# Per-skill symlinks instead of replacing the directory, so Anthropic's
# pre-landed default skills are preserved.
echo "Deploying skills to ~/.claude/skills/..."
mkdir -p ~/.claude/skills
for skill in ~/.nix-profile/share/claude-hooks/skills/*/; do
  ln -sfn "$skill" ~/.claude/skills/"$(basename "$skill")"
done

echo "Setup complete. Log: ${LOG_FILE}"
