#!/bin/bash
# Setup script for Claude Code web sessions.
#
# Installs:
#   1. Nix (via official single-user installer from nixos.org)
#   2. claude-hooks + bbapi — from flake, fetched via attic binary cache
#   3. skills — deployed to ~/.claude/skills/ from the skills flake package
#
# Usage (Claude Code web UI setup command):
#   curl -fsSL https://raw.githubusercontent.com/agentydragon/ducktape/main/devinfra/claude/web_setup.sh | bash
set -euo pipefail

LOG_FILE="/tmp/web-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

FLAKE="github:agentydragon/ducktape"

echo "Installing Nix..."
curl -fsSL https://nixos.org/nix/install | sh -s -- --no-daemon
# shellcheck disable=SC1091
. ~/.nix-profile/etc/profile.d/nix.sh

echo "Configuring Nix for gVisor (no local builds, attic cache)..."
mkdir -p ~/.config/nix
cat >>~/.config/nix/nix.conf <<'EOF'
build-users-group =
experimental-features = nix-command flakes
sandbox = false
max-jobs = 0
system-features =
substituters = https://cache.nixos.org https://cache.allegedly.works/main
trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= cache.allegedly.works-1:OX/cis8G1W13DALkGvhdUZ1OY3yGATbXw8+tIc8J7oA=
EOF

echo "Installing claude-hooks and bbapi (attic cache hit)..."
nix profile install \
  "${FLAKE}#claude-hooks" \
  "${FLAKE}#bbapi"

echo "Deploying skills to ~/.claude/skills/..."
skills=$(nix build --no-link --print-out-paths "${FLAKE}#skills")
mkdir -p ~/.claude/skills
cp -r "$skills/share/claude-hooks/skills/." ~/.claude/skills/

echo "Setup complete. Log: ${LOG_FILE}"
