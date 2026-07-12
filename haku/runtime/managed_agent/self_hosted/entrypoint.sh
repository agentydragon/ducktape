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

clone_or_pull() { # <url> <dest> [extra git clone flags...]
  if [ -d "$2/.git" ]; then
    git -C "$2" pull --ff-only
  else
    git clone "${@:3}" "$1" "$2"
  fi
}

# Behavior: ducktape's haku/base + haku/run.md, read at runtime (live-editable —
# no image rebuild to change the manual). NOT --depth 1: the run procedure's
# base-sync diffs HEAD against the last-reconciled commit (`git log <pin>..HEAD`),
# which needs that commit present. A week of history covers the wake cadence.
# (A `git log` that reaches past the pin errors with empty output; the Python
# worker turns empty tool output into "(no output)" rather than deadlocking on
# it the way `ant` did — see worker.py / anthropic-sdk-go#377.)
clone_or_pull "$HAKU_DUCKTAPE_REPO_URL" "$ducktape_dir" --shallow-since="1 week ago"
# Memory + the general durable write surface.
clone_or_pull "$HAKU_STATE_REPO_URL" "$state_dir" --depth 1
git -C "$state_dir" config user.name haku
git -C "$state_dir" config user.email haku@allegedly.works

# kubectl uses the pod's haku ServiceAccount token automatically (in-cluster);
# no kubeconfig to materialize, unlike the Claude Code web home.
#
# Long-poll the self-hosted work queue (worker.py on the anthropic Python SDK,
# baked into the image as `haku-worker`). ANTHROPIC_ENVIRONMENT_ID/_KEY come from
# the Deployment env; the workdir holds the git clones above.
export ANTHROPIC_WORKDIR="$workspace"
exec haku-worker
