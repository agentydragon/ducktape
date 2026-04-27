#!/usr/bin/env bash
# Unified setup/maintenance entrypoint for OpenAI Codex Cloud.
#
# Usage:
#   bash devinfra/codex_cloud/setup.sh --mode=install
#   bash devinfra/codex_cloud/setup.sh --mode=maintenance

set -euo pipefail

MODE="install"
for arg in "$@"; do
  case "$arg" in
    --mode=install) MODE="install" ;;
    --mode=maintenance) MODE="maintenance" ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

log() {
  printf '[codex-cloud %s] %s\n' "$MODE" "$*"
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

source_nix_profile_if_present() {
  if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
    # shellcheck disable=SC1091
    . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
  fi
}

reconcile_buildbuddy_remote() {
  # Prefer origin when it already points directly at GitHub. Fallback to
  # github-no-proxy only for proxied origin URLs.
  if git remote get-url origin >/dev/null 2>&1; then
    local origin_url
    origin_url="$(git remote get-url origin)"
    if [[ "$origin_url" == *"github.com"* ]]; then
      log "origin is GitHub; using origin for BuildBuddy remote"
      git config buildbuddy.remote-bazel-remote-name origin
    else
      local github_remote_url="https://github.com/agentydragon/ducktape"
      if git remote get-url github-no-proxy >/dev/null 2>&1; then
        log "origin is proxied; github-no-proxy already present"
      else
        git remote add github-no-proxy "$github_remote_url"
        log "origin is proxied; added github-no-proxy remote"
      fi
      git config buildbuddy.remote-bazel-remote-name github-no-proxy
    fi
  else
    log "origin remote missing; leaving buildbuddy.remote-bazel-remote-name unchanged"
  fi
}

install_nix_if_missing() {
  if command -v nix >/dev/null 2>&1; then
    log "nix already installed"
    return 0
  fi

  log "nix not found; installing Determinate Nix installer"
  local nix_installer_version="v3.17.3"
  local nix_installer_url="https://github.com/DeterminateSystems/nix-installer/releases/download/${nix_installer_version}/nix-installer-x86_64-linux"
  local nix_installer_sha256="4a84424a0a598b671de21fca1602ea3e74af214d823020afe7aac0056dc032ac"
  local nix_installer_bin="/tmp/nix-installer"

  curl -fsSL "$nix_installer_url" -o "$nix_installer_bin"
  echo "${nix_installer_sha256}  ${nix_installer_bin}" | sha256sum -c
  chmod +x "$nix_installer_bin"

  local saved_user="${USER:-}"
  export USER="${USER:-$(id -u -n)}"
  "$nix_installer_bin" install linux \
    --no-confirm \
    --init none \
    --extra-conf "sandbox = false" \
    --extra-conf "max-jobs = auto" \
    --extra-conf "system-features =" \
    --extra-conf "substituters = https://cache.nixos.org" \
    --extra-conf "trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="

  source_nix_profile_if_present

  if [ -z "$saved_user" ]; then unset USER; else USER="$saved_user"; fi
}

install_or_refresh_devtools() {
  if command -v nix >/dev/null 2>&1; then
    log "installing repo devtools profile"
    nix profile remove devtools 2>/dev/null || true
    nix profile install --max-jobs auto "path:${REPO_ROOT}#devtools"
  fi
}

run_buildbuddy_setup() {
  if [ -f devinfra/setup_buildbuddy.sh ]; then
    load_buildbuddy_api_key_from_sops
    log "running BuildBuddy setup"
    bash devinfra/setup_buildbuddy.sh
  fi
}

persist_agent_shell_init() {
  if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
    log "persisting nix-daemon profile sourcing into ~/.bashrc"
    ensure_line '. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh' ~/.bashrc
  fi

  if [ -n "${SOPS_AGE_KEY:-}" ]; then
    log "persisting SOPS_AGE_KEY into ~/.bashrc for agent shells"
    ensure_line "export SOPS_AGE_KEY='${SOPS_AGE_KEY}'" ~/.bashrc
  else
    log "SOPS_AGE_KEY not set (expected unless configured in Codex environment vars)"
  fi
}

validate_core_tools() {
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
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

log "repo=$REPO_ROOT"
log "commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)"

case "$MODE" in
  install)
    install_nix_if_missing
    install_or_refresh_devtools
    run_buildbuddy_setup
    reconcile_buildbuddy_remote
    persist_agent_shell_init
    ;;
  maintenance)
    source_nix_profile_if_present
    log "refreshing git remotes"
    git fetch --all --prune
    run_buildbuddy_setup
    reconcile_buildbuddy_remote
    install_or_refresh_devtools
    validate_core_tools
    ;;
  *)
    echo "Unsupported mode: $MODE" >&2
    exit 2
    ;;
esac

log "complete"
