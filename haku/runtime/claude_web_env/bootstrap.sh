#!/bin/bash
# Web-home bootstrap: hand Haku its kubeconfig and state repo, ready to use.
#
# Single profile background command (after_env, so K8S_* exports are in scope):
#   1. Materialize ~/.kube/config from the SOPS-encrypted haku JWT. This is the
#      one and only place the kubeconfig is built — no second writer to race.
#   2. Read the haku-state-git-write secret from haku-sandbox and write a
#      ~/.netrc so git needs no inline credentials.
#   3. Read haku-forgejo-tea from haku-sandbox and write tea's config when the
#      token rotator has published it.
#   4. Clone (or fast-forward) haku-state into ~/haku-state — outside the
#      ducktape checkout, so there's no collision. The clone runs into a temp dir
#      and is atomically swapped into place, so ~/haku-state never exists
#      half-populated (it's absent until the clone completes). An early echo +
#      the daemon's "Task [bootstrap] exited" message (bg stdout is surfaced to
#      the agent) keep an agent that starts before this finishes from mistaking
#      an absent checkout for a first run (the bug that produced duplicate items
#      on the 8th run).
set -euo pipefail

state_dir="$HOME/haku-state"
ns=haku-sandbox

python3 "$CLAUDE_PROJECT_DIR/devinfra/k8s/kubeconfig.py" --write ~/.kube/config

u=$(kubectl -n "$ns" get secret haku-state-git-write -o jsonpath='{.data.username}' | base64 -d)
p=$(kubectl -n "$ns" get secret haku-state-git-write -o jsonpath='{.data.password}' | base64 -d)

umask 077
printf 'machine git.allegedly.works login %s password %s\n' "$u" "$p" >~/.netrc

if kubectl -n "$ns" get secret haku-forgejo-tea >/dev/null 2>&1; then
  install -d -m700 "$HOME/.config/tea"
  kubectl -n "$ns" get secret haku-forgejo-tea -o jsonpath='{.data.config\.yml}' \
    | base64 -d >"$HOME/.config/tea/config.yml"
  chmod 600 "$HOME/.config/tea/config.yml"
else
  echo "haku warning: haku-forgejo-tea secret is absent; tea is installed but not logged in yet"
fi

if [ -d "$state_dir/.git" ]; then
  git -C "$state_dir" pull --ff-only || true
else
  # Clone into a temp dir and atomically swap it into place, so ~/haku-state is
  # NEVER half-populated — it simply doesn't exist until the clone completes.
  # Announce on stdout: this runs as a background command, and the hook daemon
  # surfaces bg stdout (and a "Task [bootstrap] exited N." mailbox message) to the
  # agent as a system message on its next tool call (devinfra/claude/claude_hook/
  # bg_command.rs + main.rs drain). So the agent gets an explicit "still cloning"
  # signal here and an explicit completion signal when this task exits — it must
  # not mistake an absent ~/haku-state for a first run.
  echo "haku-state: cloning in the background (pid $$). ~/haku-state is populated via atomic swap only when this completes — it is NOT ready until the 'Task [bootstrap] exited' message arrives (or until ~/haku-state/items exists)."
  tmp_dir="$HOME/haku-state.cloning"
  rm -rf "$tmp_dir"
  git clone https://git.allegedly.works/haku/haku-state.git "$tmp_dir"
  mv "$tmp_dir" "$state_dir"
fi
git -C "$state_dir" config user.name haku
git -C "$state_dir" config user.email haku@allegedly.works
echo "haku ready: ~/.kube/config materialized, haku-state at $state_dir, ~/.netrc set, tea config checked"
