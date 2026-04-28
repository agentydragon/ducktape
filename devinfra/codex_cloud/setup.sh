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

readonly NIX_DAEMON_PROFILE_SH="/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh"
readonly BAZELRC_DIR="${HOME}/.config/bazel"
readonly BBR_BAZELRC_PATH="${BAZELRC_DIR}/bbr.bazelrc"
readonly CODEX_BAZELRC_PATH="${BAZELRC_DIR}/codex.bazelrc"
readonly USER_BAZELRC_PATH="${HOME}/.bazelrc"

SOPS_RUNTIME_READY=0

ensure_line() {
  local line="$1"
  local file="$2"
  if grep -Fqx "$line" "$file" 2>/dev/null; then
    return 0
  fi
  if [ -s "$file" ] && [ "$(tail -c1 "$file" 2>/dev/null || true)" != "" ]; then
    echo >>"$file"
  fi
  echo "$line" >>"$file"
}

init_sops_runtime_prereqs() {
  if [ -z "${SOPS_AGE_KEY:-}" ]; then
    log "SOPS_AGE_KEY not set; SOPS-dependent setup steps will be skipped"
    SOPS_RUNTIME_READY=0
    return 0
  fi
  SOPS_RUNTIME_READY=1
}

load_agent_secrets() {
  if [ "$SOPS_RUNTIME_READY" -ne 1 ]; then
    return 0
  fi
  # Source the same env script claude-web uses. The codex-cloud-agent age key
  # is a recipient on every SOPS file web_env.sh decrypts, so this populates
  # BUILDBUDDY_API_KEY, GITHUB_TOKEN, DUCKTAPE_OTEL_BEARER_TOKEN, and
  # DUCKTAPE_CI_READ_GITHUB_TOKEN.
  log "sourcing devinfra/secrets/web_env.sh"
  # shellcheck source=../secrets/web_env.sh
  source "${REPO_ROOT}/devinfra/secrets/web_env.sh"
}

source_nix_profile_if_present() {
  if [ -f "$NIX_DAEMON_PROFILE_SH" ]; then
    # shellcheck disable=SC1091
    . "$NIX_DAEMON_PROFILE_SH"
  fi
}

reconcile_buildbuddy_remote() {
  # Prefer origin when it already points directly at GitHub. Fallback to
  # github-no-proxy only for proxied origin URLs.
  local github_remote_url="https://github.com/agentydragon/ducktape"
  if git remote get-url origin >/dev/null 2>&1; then
    local origin_url
    origin_url="$(git remote get-url origin)"
    if [[ "$origin_url" == *"github.com"* ]]; then
      log "origin is GitHub; using origin for BuildBuddy remote"
      git config buildbuddy.remote-bazel-remote-name origin
    else
      if git remote get-url github-no-proxy >/dev/null 2>&1; then
        log "origin is proxied; github-no-proxy already present"
      else
        git remote add github-no-proxy "$github_remote_url"
        log "origin is proxied; added github-no-proxy remote"
      fi
      git config buildbuddy.remote-bazel-remote-name github-no-proxy
    fi
  else
    if git remote get-url github-no-proxy >/dev/null 2>&1; then
      log "origin remote missing; github-no-proxy already present"
    else
      git remote add github-no-proxy "$github_remote_url"
      log "origin remote missing; added github-no-proxy remote"
    fi
    git config buildbuddy.remote-bazel-remote-name github-no-proxy
    log "origin remote missing; using github-no-proxy for BuildBuddy remote"
  fi
}

ensure_bbr_base_branch() {
  # bbr expects a local `devel` branch reference for base-branch calculations.
  # Prefer tracking the selected BuildBuddy remote's devel branch.
  local preferred_remote
  preferred_remote="$(git config --get buildbuddy.remote-bazel-remote-name || true)"
  if [ -z "$preferred_remote" ]; then
    preferred_remote="origin"
  fi

  local remote_ref=""
  if git ls-remote --exit-code --heads "$preferred_remote" devel >/dev/null 2>&1; then
    git fetch "$preferred_remote" devel >/dev/null 2>&1 || true
    if git show-ref --verify --quiet "refs/remotes/${preferred_remote}/devel"; then
      remote_ref="${preferred_remote}/devel"
    fi
  fi

  if [ -z "$remote_ref" ] && git show-ref --verify --quiet refs/remotes/origin/devel; then
    remote_ref="origin/devel"
  fi
  if [ -z "$remote_ref" ] && git show-ref --verify --quiet refs/remotes/github-no-proxy/devel; then
    remote_ref="github-no-proxy/devel"
  fi

  if [ -z "$remote_ref" ]; then
    log "local 'devel' branch missing and no remote devel ref found; bbr may fail"
    return 0
  fi

  local existing_upstream=""
  existing_upstream="$(git for-each-ref --format='%(upstream:short)' refs/heads/devel 2>/dev/null || true)"

  if git rev-parse --verify devel >/dev/null 2>&1; then
    if [ "$existing_upstream" = "$remote_ref" ]; then
      log "local 'devel' branch already tracks ${remote_ref}"
      return 0
    fi
    git branch --set-upstream-to="$remote_ref" devel >/dev/null 2>&1 || true
    log "configured local 'devel' branch to track ${remote_ref}"
    return 0
  fi

  git branch --track devel "$remote_ref" >/dev/null 2>&1 || git branch devel "$remote_ref" >/dev/null 2>&1 || true
  log "created local 'devel' branch tracking ${remote_ref} for bbr compatibility"
}

install_nix_if_missing() {
  if command -v nix >/dev/null 2>&1; then
    log "nix already installed"
    return 0
  fi

  log "nix not found; installing Determinate Nix installer"
  bash "${REPO_ROOT}/devinfra/install_nix_determinate.sh"

  source_nix_profile_if_present
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
    load_agent_secrets
    log "running BuildBuddy setup"
    bash devinfra/setup_buildbuddy.sh
  fi
}

install_precommit_hooks() {
  log "installing pre-commit hooks"
  pre-commit install --install-hooks
}

materialize_kubeconfig_if_possible() {
  local nix_python="${HOME}/.nix-profile/bin/python3"
  if [ "$SOPS_RUNTIME_READY" -ne 1 ]; then
    log "skipping kubeconfig materialization (SOPS prerequisites unavailable)"
    return 0
  fi
  log "materializing ~/.kube/config from SOPS-encrypted JWT"
  CLAUDE_PROJECT_DIR="${REPO_ROOT}" "$nix_python" "${REPO_ROOT}/devinfra/k8s/kubeconfig.py" --write "${HOME}/.kube/config"
}

write_codex_bazelrcs() {
  local session_tag="${CODEX_SESSION_ID:-codex-cloud}"

  mkdir -p "$BAZELRC_DIR"

  cat >"$BBR_BAZELRC_PATH" <<EOF
# Auto-generated by devinfra/codex_cloud/setup.sh
build --build_metadata=ROLE=codex-cloud
build --build_metadata=TAGS=session:${session_tag}
EOF

  cat >"$CODEX_BAZELRC_PATH" <<EOF
# Auto-generated by devinfra/codex_cloud/setup.sh
test --test_tag_filters=-live_openai_api
try-import ${HOME}/.config/bazel/buildbuddy.bazelrc
try-import ${BBR_BAZELRC_PATH}
common --config=ai_agent
EOF
}

persist_agent_shell_init() {
  local bashrc="${HOME}/.bashrc"
  local bash_profile="${HOME}/.bash_profile"
  local shell_file="$bashrc"

  touch "$bashrc"
  touch "$bash_profile"
  touch "$USER_BAZELRC_PATH"

  # Ensure login shells (used by many agent command runners) load ~/.bashrc.
  ensure_line '[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"' "$bash_profile"

  if [ -f "$NIX_DAEMON_PROFILE_SH" ]; then
    log "persisting nix-daemon profile sourcing into ~/.bashrc"
    ensure_line ". ${NIX_DAEMON_PROFILE_SH}" "$shell_file"
    ensure_line 'export PATH="$HOME/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH"' "$shell_file"
  fi

  if [ -n "${SOPS_AGE_KEY:-}" ]; then
    log "persisting SOPS_AGE_KEY into ~/.bashrc for agent shells"
    ensure_line "export SOPS_AGE_KEY='${SOPS_AGE_KEY}'" "$shell_file"
  else
    log "SOPS_AGE_KEY not set (expected unless configured in Codex environment vars)"
  fi

  if [ -n "${BUILDBUDDY_API_KEY:-}" ]; then
    log "persisting BUILDBUDDY_API_KEY into ~/.bashrc for agent shells"
    ensure_line "export BUILDBUDDY_API_KEY='${BUILDBUDDY_API_KEY}'" "$shell_file"
  else
    log "BUILDBUDDY_API_KEY not set at setup time; runtime sourcing will be used when possible"
  fi

  log "persisting runtime secret hydration and bazel env into ~/.bashrc"
  ensure_line "[ -n \"\${SOPS_AGE_KEY:-}\" ] && [ -f \"${REPO_ROOT}/devinfra/secrets/web_env.sh\" ] && source \"${REPO_ROOT}/devinfra/secrets/web_env.sh\" >/dev/null 2>&1 || true" "$shell_file"
  ensure_line "export BBR_BAZELRC='${BBR_BAZELRC_PATH}'" "$shell_file"
  ensure_line "export SESSION_BAZELRC='${CODEX_BAZELRC_PATH}'" "$shell_file"
  ensure_line "try-import ${CODEX_BAZELRC_PATH}" "$USER_BAZELRC_PATH"
}

run_common_setup_steps() {
  install_or_refresh_devtools
  run_buildbuddy_setup
  install_precommit_hooks
  write_codex_bazelrcs
  reconcile_buildbuddy_remote
  ensure_bbr_base_branch
  materialize_kubeconfig_if_possible
}

validate_core_tools() {
  bb --version >/dev/null
  log "bb present"
  bbr --help >/dev/null
  log "bbr present"
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
init_sops_runtime_prereqs

log "repo=$REPO_ROOT"
log "commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)"

case "$MODE" in
  install)
    install_nix_if_missing
    run_common_setup_steps
    persist_agent_shell_init
    ;;
  maintenance)
    source_nix_profile_if_present
    log "refreshing git remotes"
    git fetch --all --prune
    run_common_setup_steps
    persist_agent_shell_init
    validate_core_tools
    ;;
  *)
    echo "Unsupported mode: $MODE" >&2
    exit 2
    ;;
esac

log "complete"
