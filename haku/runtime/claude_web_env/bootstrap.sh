#!/bin/bash
# Web-home bootstrap: hand Haku its kubeconfig and state repo, ready to use.
#
# Single profile background command (after_env, so K8S_* exports are in scope):
#   1. Materialize ~/.kube/config from the SOPS-encrypted haku JWT. This is the
#      one and only place the kubeconfig is built — no second writer to race.
#   2. Read the haku-state-git-write secret from haku-sandbox and write a
#      ~/.netrc so git needs no inline credentials.
#   3. Clone (or fast-forward) haku-state into ~/haku-state — outside the
#      ducktape checkout, so there's no collision.
set -euo pipefail

state_dir="$HOME/haku-state"
ns=haku-sandbox

python3 "$CLAUDE_PROJECT_DIR/devinfra/k8s/kubeconfig.py" --write ~/.kube/config

u=$(kubectl -n "$ns" get secret haku-state-git-write -o jsonpath='{.data.username}' | base64 -d)
p=$(kubectl -n "$ns" get secret haku-state-git-write -o jsonpath='{.data.password}' | base64 -d)

umask 077
printf 'machine git.allegedly.works login %s password %s\n' "$u" "$p" >~/.netrc

if [ -d "$state_dir/.git" ]; then
  git -C "$state_dir" pull --ff-only || true
else
  git clone https://git.allegedly.works/haku/haku-state.git "$state_dir"
fi
git -C "$state_dir" config user.name haku
git -C "$state_dir" config user.email haku@allegedly.works
echo "haku ready: ~/.kube/config materialized, haku-state at $state_dir, ~/.netrc set"
