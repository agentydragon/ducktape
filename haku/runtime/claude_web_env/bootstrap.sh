#!/bin/bash
# Web-home bootstrap: hand Haku its kubeconfig and state repo, ready to use.
#
# Single profile background command (after_env, so K8S_* exports are in scope):
#   1. Materialize ~/.kube/config from the SOPS-encrypted haku JWT. This is the
#      one and only place the kubeconfig is built — no second writer to race.
#   2. Read the haku-forgejo-git secret from haku-sandbox and write a
#      ~/.netrc so git needs no inline credentials.
#   3. Read haku-forgejo-tea from haku-sandbox and write tea's config when the
#      token rotator has published it.
#   4. Read haku-mail-token and write himalaya's config with the current mail
#      JWT embedded (the token rotates; each session materializes it fresh).
#      The IMAP host is cluster-internal — from the web home read mail via
#      JMAP, or relay IMAP through kubectl exec; the config is still landed
#      here so every runtime shares one recipe.
#   5. Clone (or fast-forward) haku-state into ~/haku-state — outside the
#      ducktape checkout, so there's no collision. The clone runs into a temp dir
#      and is atomically swapped into place, so ~/haku-state never exists
#      half-populated (it's absent until the clone completes). An early echo +
#      the daemon's "Task [bootstrap] exited" message (bg stdout is surfaced to
#      the agent) keep an agent that starts before this finishes from mistaking
#      an absent checkout for a first run (the bug that produced duplicate items
#      on the 8th run).
set -euo pipefail

# Managed/task-runner sessions ("execute haku run.md" task sessions, as opposed to an
# interactive web-home session) don't run the claude-hook daemon, so profile.yaml's
# env_exports never apply and this script ends up invoked directly with none of its
# expected env. Default everything it needs so it's self-sufficient in that harness;
# a real web-home session's profile-provided values still win (haku-state items
# haku-bootstrap-claude-project-dir-unbound-2026,
# haku-session-start-hook-absent-managed-run-2026-07).
: "${CLAUDE_PROJECT_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
: "${K8S_JWT_SOPS_PATH:=secrets/haku-k8s-jwt.yaml}"
: "${K8S_USER:=haku}"
: "${K8S_NAMESPACE:=haku-sandbox}"
export CLAUDE_PROJECT_DIR K8S_JWT_SOPS_PATH K8S_USER K8S_NAMESPACE

state_dir="$HOME/haku-state"
ns=haku-sandbox

python3 "$CLAUDE_PROJECT_DIR/devinfra/k8s/kubeconfig.py" --write ~/.kube/config

u=$(kubectl -n "$ns" get secret haku-forgejo-git -o jsonpath='{.data.username}' | base64 -d)
p=$(kubectl -n "$ns" get secret haku-forgejo-git -o jsonpath='{.data.password}' | base64 -d)

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

# Canonical himalaya config recipe (haku-state's sources/mailbox.md points here).
# Verified against the live server (himalaya 1.1.0 + oauth2 feature): `pkce` and
# `scope` are required by the config schema; auth-url/token-url are schema-
# required but never contacted; the token is embedded as a `raw` secret because
# the `cmd` secret form fails ("cannot get secret from command: empty output"
# in the refresh branch) — hence re-materializing per session as the JWT rotates.
if mail_tok=$(kubectl -n "$ns" get secret haku-mail-token -o jsonpath='{.data.jwt}' 2>/dev/null | base64 -d) && [ -n "$mail_tok" ]; then
  install -d -m700 "$HOME/.config/himalaya"
  cat >"$HOME/.config/himalaya/config.toml" <<EOF
[accounts.haku]
default = true
email = "haku@allegedly.works"
backend.type = "imap"
backend.host = "haku-mailbox.haku-mailbox.svc.cluster.local"
backend.port = 1143
backend.encryption.type = "none"
backend.login = "haku@allegedly.works"
backend.auth.type = "oauth2"
backend.auth.method = "oauthbearer"
backend.auth.pkce = false
backend.auth.scope = "openid"
backend.auth.client-id = "stalwart-haku"
backend.auth.auth-url = "https://auth.allegedly.works/application/o/authorize/"
backend.auth.token-url = "https://auth.allegedly.works/application/o/token/"
backend.auth.access-token.raw = "$mail_tok"
EOF
  chmod 600 "$HOME/.config/himalaya/config.toml"
else
  echo "haku warning: haku-mail-token secret is absent/empty; himalaya not configured"
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
