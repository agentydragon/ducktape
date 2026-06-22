#!/usr/bin/env bash
# haku-worker entrypoint (runs in haku-sandbox): clone Haku's home into the agent
# workdir, then long-poll Anthropic's self-hosted work queue with the fixed `ant`
# toolset. The pod holds ONLY the environment key (sk-ant-oat01-...), never the
# org-scoped API key — so a prompt-injected tool call can't reach the control plane.
set -euo pipefail

workspace=/workspace
ducktape_dir="$workspace/ducktape"
state_dir="$workspace/haku-state"

# git auth for the Forgejo host, off any command line.
umask 077
printf 'machine %s login %s password %s\n' \
  "$HAKU_GIT_HOST" "$HAKU_GIT_USERNAME" "$HAKU_GIT_PASSWORD" >"$HOME/.netrc"

clone_or_pull() { # <url> <dest>
  if [ -d "$2/.git" ]; then
    git -C "$2" pull --ff-only
  else
    git clone --depth 1 "$1" "$2"
  fi
}

# Behavior: ducktape's haku/base + haku/run.md, read at runtime (live-editable —
# no image rebuild to change the manual).
clone_or_pull "$HAKU_DUCKTAPE_REPO_URL" "$ducktape_dir"
# Memory + the only write surface.
clone_or_pull "$HAKU_STATE_REPO_URL" "$state_dir"
git -C "$state_dir" config user.name haku
git -C "$state_dir" config user.email haku@allegedly.works

# kubectl uses the pod's haku ServiceAccount token automatically (in-cluster);
# no kubeconfig to materialize, unlike the Claude Code web home.
exec ant beta:worker poll \
  --environment-id "$ANTHROPIC_ENVIRONMENT_ID" \
  --workdir "$workspace"
