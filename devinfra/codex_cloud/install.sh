#!/usr/bin/env bash
# Setup script for OpenAI Codex Cloud environment runs.
#
# Usage in Codex Cloud environment config:
#   bash devinfra/codex_cloud/install.sh

set -euo pipefail

log() {
  printf '[codex-cloud install] %s\n' "$*"
}

ensure_line() {
  local line="$1"
  local file="$2"
  grep -Fqx "$line" "$file" 2>/dev/null || echo "$line" >>"$file"
}

load_buildbuddy_api_key_from_sops() {
  local sops_file="cluster/k8s/agents/shared-secrets/buildbuddy-api-key.sops.yaml"
  if [ -n "${BUILDBUDDY_API_KEY:-}" ]; then
    log "BUILDBUDDY_API_KEY already set"
    return 0
  fi
  if [ -z "${SOPS_AGE_KEY:-}" ]; then
    log "SOPS_AGE_KEY not set; cannot decrypt BuildBuddy API key"
    return 0
  fi
  if ! command -v sops >/dev/null 2>&1; then
    log "sops command missing; cannot decrypt BuildBuddy API key"
    return 0
  fi
  if [ ! -f "$sops_file" ]; then
    log "BuildBuddy sops file missing at $sops_file"
    return 0
  fi

  log "decrypting BuildBuddy API key from sops file"
  BUILDBUDDY_API_KEY="$(
    sops -d "$sops_file" | python3 -c 'import sys, yaml; print(yaml.safe_load(sys.stdin)["stringData"]["api-key"])'
  )"
  export BUILDBUDDY_API_KEY
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

log "repo=$REPO_ROOT"
log "commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)"

# 1) Optional Nix bootstrap for closer parity with canonical local workflows.
#    Safe to skip if unavailable; Bazel+bbr remains the primary execution path.
if ! command -v nix >/dev/null 2>&1; then
  log "nix not found; installing Determinate Nix installer"
  NIX_INSTALLER_VERSION="v3.17.3"
  NIX_INSTALLER_URL="https://github.com/DeterminateSystems/nix-installer/releases/download/${NIX_INSTALLER_VERSION}/nix-installer-x86_64-linux"
  NIX_INSTALLER_SHA256="4a84424a0a598b671de21fca1602ea3e74af214d823020afe7aac0056dc032ac"
  NIX_INSTALLER_BIN="/tmp/nix-installer"

  curl -fsSL "$NIX_INSTALLER_URL" -o "$NIX_INSTALLER_BIN"
  echo "${NIX_INSTALLER_SHA256}  ${NIX_INSTALLER_BIN}" | sha256sum -c
  chmod +x "$NIX_INSTALLER_BIN"

  _saved_user="${USER:-}"
  export USER="${USER:-$(id -u -n)}"
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
  if [ -z "$_saved_user" ]; then unset USER; else USER="$_saved_user"; fi
  unset _saved_user
else
  log "nix already installed"
fi

if command -v nix >/dev/null 2>&1; then
  log "installing repo devtools profile"
  nix profile remove devtools 2>/dev/null || true
  nix profile install --max-jobs auto "path:${REPO_ROOT}#devtools"
fi

# 2) BuildBuddy bootstrap from repository SSOT.
if [ -f devinfra/setup_buildbuddy.sh ]; then
  load_buildbuddy_api_key_from_sops
  log "running BuildBuddy setup"
  bash devinfra/setup_buildbuddy.sh
fi

# 3) Git remote for bbr runner clone behavior.
# Prefer origin when it already points directly at GitHub. Fallback to
# github-no-proxy only for proxied origin URLs.
if git remote get-url origin >/dev/null 2>&1; then
  ORIGIN_URL="$(git remote get-url origin)"
  if [[ "$ORIGIN_URL" == *"github.com"* ]]; then
    log "origin is already GitHub; using origin for BuildBuddy remote"
    git config buildbuddy.remote-bazel-remote-name origin
  else
    GITHUB_REMOTE_URL="https://github.com/agentydragon/ducktape"
    if git remote get-url github-no-proxy >/dev/null 2>&1; then
      log "origin is proxied; github-no-proxy already present"
    else
      git remote add github-no-proxy "$GITHUB_REMOTE_URL"
      log "origin is proxied; added github-no-proxy remote"
    fi
    git config buildbuddy.remote-bazel-remote-name github-no-proxy
  fi
else
  log "origin remote missing; leaving buildbuddy.remote-bazel-remote-name unchanged"
fi

# 4) Persist shell init that Codex agent Bash sessions will load.
if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
  log "persisting nix-daemon profile sourcing into ~/.bashrc"
  ensure_line '. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh' ~/.bashrc
fi

# 5) SOPS: if SOPS_AGE_KEY is present in environment configuration,
#    persist to bashrc so agent shells inherit it.
if [ -n "${SOPS_AGE_KEY:-}" ]; then
  log "persisting SOPS_AGE_KEY into ~/.bashrc for agent shells"
  ensure_line "export SOPS_AGE_KEY='${SOPS_AGE_KEY}'" ~/.bashrc
else
  log "SOPS_AGE_KEY not set (expected unless configured in Codex environment vars)"
fi

log "setup complete"
