#!/usr/bin/env bash
# Shared bb-remote git-remote reconciliation for Claude web and Codex Cloud
# session setup. Source this file (after defining a `log()` function) and
# call `reconcile_bbr_remote <repo_dir>`.
#
# Fixes two problems that break `bbr`/`bb remote` in sandboxed sessions (see
# devinfra/docs/bb_remote_internals.md "Gotchas" for the full explanation):
#
# 1. Sandboxed sessions rewrite `origin` to a local git-mirroring proxy
#    (http://127.0.0.1:<port>/git/...) so the cloud runner can't fetch from
#    it directly. Fix: a 'github-no-proxy' remote pointing straight at
#    GitHub, selected via `buildbuddy.remote-bazel-remote-name` (bb resolves
#    the URL via `git remote get-url`, not the literal config value).
# 2. Some sessions ALSO install a *global* git `insteadOf` rewrite of any
#    literal "https://github.com/..." URL (repo-access scoping) — this
#    rewrites `github-no-proxy`'s URL too, silently routing it back through
#    the same unreachable proxy. Fix: give it a URL that's real but doesn't
#    match that literal prefix (the explicit default port, ":443" — same
#    URL, different string).
#
# Also keeps the local default-branch (e.g. "devel" in this repo, but
# determined dynamically via `git ls-remote --symref` — never assumed) tracking
# a real remote ref: `bb remote` falls back to `<default-branch>@{upstream}` as
# its diff base when the current branch isn't tracked on the BuildBuddy remote,
# and a stale local tracking ref there produces a huge or unappliable patchset.
# `devinfra/bbr.py`'s `check_base_branch_freshness` warns about this on every
# `bbr` call without ever fetching (surprise network calls on every command
# would be worse); this setup-time step is the one place that *does* fetch,
# since session setup already does other one-time network installs.

reconcile_bbr_remote() {
  local repo_dir="$1"
  local github_remote_url="https://github.com:443/agentydragon/ducktape"

  if git -C "$repo_dir" remote get-url origin >/dev/null 2>&1 \
    && [[ "$(git -C "$repo_dir" remote get-url origin)" == *"github.com"* ]]; then
    log "origin is GitHub; using origin for BuildBuddy remote"
    git -C "$repo_dir" config buildbuddy.remote-bazel-remote-name origin
  else
    if git -C "$repo_dir" remote get-url github-no-proxy >/dev/null 2>&1; then
      git -C "$repo_dir" remote set-url github-no-proxy "$github_remote_url"
      log "git remote 'github-no-proxy' already exists, updated URL -> $github_remote_url"
    else
      git -C "$repo_dir" remote add github-no-proxy "$github_remote_url"
      log "Added git remote 'github-no-proxy' -> $github_remote_url"
    fi
    git -C "$repo_dir" config buildbuddy.remote-bazel-remote-name github-no-proxy
    log "Set buildbuddy.remote-bazel-remote-name=github-no-proxy"
  fi

  _ensure_bbr_base_branch "$repo_dir"
}

_ensure_bbr_base_branch() {
  local repo_dir="$1"
  local preferred_remote
  preferred_remote="$(git -C "$repo_dir" config --get buildbuddy.remote-bazel-remote-name || true)"
  if [ -z "$preferred_remote" ]; then
    preferred_remote="origin"
  fi

  # Ask the remote for its actual default branch rather than assuming a name
  # — this is a one-time setup-time network call (unlike bbr.py, which never
  # fetches on its own), so the small extra round-trip is fine here.
  local default_branch=""
  default_branch="$(git -C "$repo_dir" ls-remote --symref "$preferred_remote" HEAD 2>/dev/null \
    | awk '/^ref:/ {sub(/^refs\/heads\//, "", $2); print $2}')"
  if [ -z "$default_branch" ]; then
    log "could not determine ${preferred_remote}'s default branch; assuming 'devel' (this repo's convention)"
    default_branch="devel"
  fi

  local remote_ref=""
  if git -C "$repo_dir" ls-remote --exit-code --heads "$preferred_remote" "$default_branch" >/dev/null 2>&1; then
    git -C "$repo_dir" fetch "$preferred_remote" "$default_branch" >/dev/null 2>&1 || true
    if git -C "$repo_dir" show-ref --verify --quiet "refs/remotes/${preferred_remote}/${default_branch}"; then
      remote_ref="${preferred_remote}/${default_branch}"
    fi
  fi

  if [ -z "$remote_ref" ] && git -C "$repo_dir" show-ref --verify --quiet "refs/remotes/origin/${default_branch}"; then
    remote_ref="origin/${default_branch}"
  fi
  if [ -z "$remote_ref" ] && git -C "$repo_dir" show-ref --verify --quiet "refs/remotes/github-no-proxy/${default_branch}"; then
    remote_ref="github-no-proxy/${default_branch}"
  fi

  if [ -z "$remote_ref" ]; then
    log "local '${default_branch}' branch missing and no remote ${default_branch} ref found; bbr may fail"
    return 0
  fi

  local existing_upstream=""
  existing_upstream="$(git -C "$repo_dir" for-each-ref --format='%(upstream:short)' "refs/heads/${default_branch}" 2>/dev/null || true)"

  if git -C "$repo_dir" rev-parse --verify "$default_branch" >/dev/null 2>&1; then
    if [ "$existing_upstream" = "$remote_ref" ]; then
      log "local '${default_branch}' branch already tracks ${remote_ref}"
      return 0
    fi
    git -C "$repo_dir" branch --set-upstream-to="$remote_ref" "$default_branch" >/dev/null 2>&1 || true
    log "configured local '${default_branch}' branch to track ${remote_ref}"
    return 0
  fi

  git -C "$repo_dir" branch --track "$default_branch" "$remote_ref" >/dev/null 2>&1 \
    || git -C "$repo_dir" branch "$default_branch" "$remote_ref" >/dev/null 2>&1 || true
  log "created local '${default_branch}' branch tracking ${remote_ref} for bbr compatibility"
}
