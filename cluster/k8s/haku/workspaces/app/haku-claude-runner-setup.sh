#!/usr/bin/env bash
# Agent-owned setup for the Console-launched Claude runtime.
#
# This deliberately lives beside the Claude SandboxTemplate rather than in the shared runner
# image. The image supplies tools; this script supplies this Agent's workspace, git identity, and
# credentials. It is safe to run again for an existing claim.
set -euo pipefail

bundle="${EGRESS_CA:-/egress-proxy-ca/ca-certificates.crt}"

# The runner receives the claim-owned session bearer in its environment. Use it as the userinfo
# credential for the colocated egress fence so git checkouts and Claude's child processes take the
# same admitted path as the runner protocol.
session_token="${HAKU_SESSION_TOKEN:-${HAKU_RUNNER_TOKEN:-}}"
if [ -n "$session_token" ]; then
  if [ ! -r "$bundle" ]; then
    echo "haku-claude-runner-setup: fenced runtime requires egress CA bundle at $bundle" >&2
    exit 1
  fi
  fence_proxy="http://haku-egress-proxy.haku-console.svc.cluster.local:8888"
  export HTTP_PROXY="http://:${session_token}@${fence_proxy#http://}"
  export HTTPS_PROXY="$HTTP_PROXY"
  export http_proxy="$HTTP_PROXY"
  export https_proxy="$HTTP_PROXY"
  export NO_PROXY="127.0.0.1,localhost,haku-console.haku-console.svc.cluster.local," \
    "haku-kube-api-proxy.haku-console.svc.cluster.local"
  export no_proxy="$NO_PROXY"
  export SSL_CERT_FILE="$bundle"
  export CURL_CA_BUNDLE="$bundle"
  export REQUESTS_CA_BUNDLE="$bundle"
  export NODE_EXTRA_CA_CERTS="$bundle"
fi

# Git's OpenSSL client does not use SSL_CERT_FILE or CURL_CA_BUNDLE for its CA file.
git config --global user.name haku
git config --global user.email haku@allegedly.works
if [ -r "$bundle" ]; then
  git config --global http.sslCAInfo "$bundle"
fi

# These placeholders are replaced by the colocated egress fence only for approved origins.
umask 077
cat >"$HOME/.netrc" <<NETRC
machine forgejo-http.forgejo
login ${HAKU_GIT_USERNAME:?HAKU_GIT_USERNAME unset}
password ${HAKU_GIT_PASSWORD:?HAKU_GIT_PASSWORD unset}
machine git.allegedly.works
login ${HAKU_GIT_USERNAME}
password ${HAKU_GIT_PASSWORD}
NETRC

if [ -n "${GITHUB_TOKEN:-}" ]; then
  cat >>"$HOME/.netrc" <<NETRC
machine github.com
login x-access-token
password ${GITHUB_TOKEN}
NETRC
fi

repo="${HAKU_STATE_DIR:-/workspace/haku-state}"
url="${HAKU_STATE_URL:-http://forgejo-http.forgejo:3000/haku/haku-state.git}"
if [ -d "$repo/.git" ]; then
  git -C "$repo" fetch --depth 1 --prune origin main
  git -C "$repo" checkout -B main origin/main
  git -C "$repo" reset --hard origin/main
else
  git clone --depth 1 --branch main --single-branch "$url" "$repo"
fi

# ducktape is a public, read-only source used for context and follow-up work. Keep its history
# available while avoiding a full blob download for every new claim.
dt_repo="${HAKU_DUCKTAPE_DIR:-/workspace/ducktape}"
dt_url="${HAKU_DUCKTAPE_URL:-https://github.com/agentydragon/ducktape.git}"
dt_branch="${HAKU_DUCKTAPE_BRANCH:-devel}"
if [ -d "$dt_repo/.git" ]; then
  git -C "$dt_repo" fetch --prune origin "$dt_branch" \
    && git -C "$dt_repo" checkout -B "$dt_branch" "origin/$dt_branch" \
    && git -C "$dt_repo" reset --hard "origin/$dt_branch" \
    || echo "haku-claude-runner-setup: ducktape refresh failed — source unavailable this claim" >&2
else
  git clone --filter=blob:none --single-branch --branch "$dt_branch" "$dt_url" "$dt_repo" \
    || echo "haku-claude-runner-setup: ducktape clone failed — source unavailable this claim" >&2
fi

echo "haku-claude-runner-setup: git identity + credentials written, haku-state + ducktape synced"
