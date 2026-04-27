#!/usr/bin/env bash
# Maintenance script for OpenAI Codex Cloud container-cache resume.
#
# Usage in Codex Cloud environment config:
#   bash devinfra/codex_cloud/maintenance.sh

set -euo pipefail

log() {
  printf '[codex-cloud maintenance] %s\n' "$*"
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

if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
  # shellcheck disable=SC1091
  . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
fi

log "refreshing git remotes"
git fetch --all --prune

if [ -f devinfra/setup_buildbuddy.sh ]; then
  load_buildbuddy_api_key_from_sops
  log "refreshing BuildBuddy config"
  bash devinfra/setup_buildbuddy.sh
fi

# Keep BuildBuddy remote selection aligned with current remotes.
if git remote get-url origin >/dev/null 2>&1; then
  ORIGIN_URL="$(git remote get-url origin)"
  if [[ "$ORIGIN_URL" == *"github.com"* ]]; then
    log "origin is GitHub; setting BuildBuddy remote to origin"
    git config buildbuddy.remote-bazel-remote-name origin
  elif git remote get-url github-no-proxy >/dev/null 2>&1; then
    log "origin is proxied; keeping github-no-proxy for BuildBuddy remote"
    git config buildbuddy.remote-bazel-remote-name github-no-proxy
  else
    log "origin is proxied but github-no-proxy is missing"
  fi
fi

if command -v nix >/dev/null 2>&1; then
  log "refreshing devtools profile"
  nix profile remove devtools 2>/dev/null || true
  nix profile install --max-jobs auto "path:${REPO_ROOT}#devtools"
fi

# Validate core commands used in AGENTS guidance.
if command -v bb >/dev/null 2>&1; then
  bb --version >/dev/null
  log "bb present"
else
  log "WARNING: bb missing"
fi

if command -v bbr >/dev/null 2>&1; then
  bbr --help >/dev/null
  log "bbr present"
else
  log "WARNING: bbr missing"
fi

log "maintenance complete"
