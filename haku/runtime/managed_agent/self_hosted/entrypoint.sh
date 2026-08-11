#!/usr/bin/env bash
# haku-managed-agent entrypoint (runs in haku-sandbox): clone Haku's home into the agent
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

# Behavior lives in the haku-state checkout, not here; ducktape is cloned because
# it is one of Haku's information sources — its recent history is read for
# follow-up work (haku-state `sources/ducktape.md`). NOT --depth 1: that read
# spans roughly the last two weeks, which a shallow clone cannot serve. A generous
# fixed depth covers the wake cadence
# (~25 commits/day on devel, so 500 is weeks of headroom).
# (A `git log` that reaches past the pin errors with empty output; the Python
# worker turns empty tool output into "(no output)" rather than deadlocking on
# it the way `ant` did — see worker.py / anthropic-sdk-go#377.)
# NOT --shallow-since: Forgejo 15.0.3 (gitea-1.22.0)'s git-http-backend can't
# process a protocol v2 deepen-since request — clone/fetch fails every time with
# `fatal: error processing shallow info: 4` (confirmed against the in-cluster
# forgejo-http.forgejo instance 2026-07-18; --depth N is unaffected).
clone_or_pull "$HAKU_DUCKTAPE_REPO_URL" "$ducktape_dir" --depth 500
# Memory + the general durable write surface.
clone_or_pull "$HAKU_STATE_REPO_URL" "$state_dir" --depth 1
git -C "$state_dir" config user.name haku
git -C "$state_dir" config user.email haku@allegedly.works

# kubectl uses the pod's haku ServiceAccount token automatically (in-cluster);
# no kubeconfig to materialize, unlike the Claude Code web home.
#
# Long-poll the self-hosted work queue (worker.py on the anthropic Python SDK,
# baked into the image as `haku-managed-agent`). ANTHROPIC_ENVIRONMENT_ID/_KEY come from
# the Deployment env; the workdir holds the git clones above.
export ANTHROPIC_WORKDIR="$workspace"
exec haku-managed-agent
