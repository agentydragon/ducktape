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

FLAKE="path:$(pwd)"

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

echo "[$(date -Iseconds)] Installing Nix..."
# Pinned to nix-2.28.3 (version-specific URL, not mutable nixos.org/nix/install).
# Anthropic's egress proxy caches responses; using a pinned version-specific URL
# avoids serving a stale tarball whose hash no longer matches the installer's expectation.
# Installer script hash verified before execution.
NIX_VERSION="2.28.3"
NIX_INSTALLER_URL="https://releases.nixos.org/nix/nix-${NIX_VERSION}/install"
NIX_INSTALLER_SHA256="46b8d7165dceb471f4346366b3a93f1009407b99729b843b8664918f4cc800a0"
# Ensure $USER is set — the installer's nix.sh (sourced below) is a no-op
# when $USER is empty, which means PATH never gets ~/.nix-profile/bin.
# The container runs as root but may not have $USER in the environment.
_saved_user="${USER:-}"
export USER="${USER:-$(id -u -n)}"
curl -fsSL "$NIX_INSTALLER_URL" -o /tmp/nix-install.sh
echo "${NIX_INSTALLER_SHA256}  /tmp/nix-install.sh" | sha256sum -c
# Pre-download the tarball and save it for post-mortem inspection.
# The installer re-downloads it internally, but saves to a temp dir that gets cleaned up.
# This copy persists in /tmp for the session so we can inspect what the proxy served.
NIX_TARBALL="nix-${NIX_VERSION}-x86_64-linux.tar.xz"
NIX_TARBALL_URL="https://releases.nixos.org/nix/nix-${NIX_VERSION}/${NIX_TARBALL}"
NIX_TARBALL_SAVE="/tmp/${NIX_TARBALL}"
echo "Pre-downloading Nix tarball for inspection: ${NIX_TARBALL_URL}"
curl -fsSL "$NIX_TARBALL_URL" -o "$NIX_TARBALL_SAVE"
echo "Nix tarball saved to: ${NIX_TARBALL_SAVE}"
echo "Nix tarball SHA256: $(sha256sum "$NIX_TARBALL_SAVE")"
sh /tmp/nix-install.sh --no-daemon
# shellcheck disable=SC1091
. ~/.nix-profile/etc/profile.d/nix.sh
# Restore $USER to its original state so we don't leak a side-effect.
if [ -z "$_saved_user" ]; then unset USER; else USER="$_saved_user"; fi
unset _saved_user
echo "[$(date -Iseconds)] Nix installation complete."

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

echo "--- nix.conf ---"
cat ~/.config/nix/nix.conf
echo "--- nix show-config ---"
nix show-config 2>/dev/null | grep -E "max-jobs|sandbox|build-users" || true
echo "---"

echo "[$(date -Iseconds)] Installing web session tools..."
# Pass --max-jobs explicitly to override any nix.conf misconfiguration.
nix profile install --max-jobs auto "${FLAKE}#web-session"
echo "[$(date -Iseconds)] Web session tools installed."

# Symlink all Nix-installed binaries into /usr/local/bin so they're on PATH.
# Claude Code is launched directly (not via login shell), so ~/.nix-profile/bin
# is not in PATH when hooks run. /usr/local/bin is always in PATH.
for bin in ~/.nix-profile/bin/*; do
  ln -sfn "$bin" /usr/local/bin/"$(basename "$bin")"
done

# --- Step 4: Symlink skills into ~/.claude/skills/ ---
# Per-skill symlinks instead of replacing the directory, so Anthropic's
# pre-landed default skills are preserved.
echo "Deploying skills to ~/.claude/skills/..."
mkdir -p ~/.claude/skills
for skill in ~/.nix-profile/share/claude-hooks/skills/*/; do
  ln -sfn "$skill" ~/.claude/skills/"$(basename "$skill")"
done

echo "[$(date -Iseconds)] Setup complete. Log: ${LOG_FILE}"
