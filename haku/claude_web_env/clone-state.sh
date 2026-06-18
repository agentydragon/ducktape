#!/bin/bash
# Web-home bootstrap: hand Haku its state repo, ready to use.
#
# Run as a profile background command AFTER the kubeconfig is materialized.
# Reads the haku-state-git-write secret from haku-sandbox, writes a ~/.netrc so
# git needs no inline credentials, then clones (or fast-forwards) haku-state into
# $CLAUDE_PROJECT_DIR/state. The agent works under ./state/ and pushes to main.
set -euo pipefail

state_dir="${CLAUDE_PROJECT_DIR:?CLAUDE_PROJECT_DIR not set}/state"
ns=haku-sandbox

u=$(kubectl -n "$ns" get secret haku-state-git-write -o jsonpath='{.data.username}' | base64 -d)
p=$(kubectl -n "$ns" get secret haku-state-git-write -o jsonpath='{.data.password}' | base64 -d)

umask 077
printf 'machine git.allegedly.works login %s password %s\n' "$u" "$p" >~/.netrc

# Keep the state checkout out of ducktape's git status (local-only exclude).
exclude="$CLAUDE_PROJECT_DIR/.git/info/exclude"
grep -qxF '/state/' "$exclude" 2>/dev/null || echo '/state/' >>"$exclude"

if [ -d "$state_dir/.git" ]; then
  git -C "$state_dir" pull --ff-only || true
else
  git clone https://git.allegedly.works/haku/haku-state.git "$state_dir"
fi
git -C "$state_dir" config user.name haku
git -C "$state_dir" config user.email haku@allegedly.works
echo "haku-state ready at \$CLAUDE_PROJECT_DIR/state (~/.netrc set for git.allegedly.works)"
