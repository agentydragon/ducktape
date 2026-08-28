#!/usr/bin/env bash
# Per-claim setup for a Haku sandbox: egress CA trust, git identity + credentials, and the
# haku-state checkout. Invoked (post-adoption, once per claim) by the sandbox-provisioning
# Console's configured bootstrap.script, which is just a call to this file — the whole reviewed
# bootstrap lives here rather than as a bash blob inside config YAML, so shfmt/shellcheck
# see it. Deviation: changing any of it now needs an image rebuild + rollout, where the
# in-YAML version was a ConfigMap edit picked up on the next claim. Accepted — the
# environment provenance never covered the image tag either way
# (haku/sandbox/config.py), so no drift detection is lost.
#
# TWO IMAGES RUN THIS, and a change here lands in both:
#   - the haku-sandbox exec target (this directory's Dockerfile), via Console's bootstrap;
#   - the Console-owned Claude runner (//haku/runner:runner_image),
#     which runs it itself before launching Claude Code, so that session comes up with
#     Haku's manual and its git credential rather than an empty /workspace.
# The steps a given box does not want are switched off by env, below — never by a second
# copy of this file, because "similar setup to the haku sandbox" is the whole requirement
# and two copies would drift out of it.
#
# Idempotent: safe to re-run against an already-set-up box.
set -euo pipefail

bundle="${EGRESS_CA:-/egress-proxy-ca/ca-certificates.crt}"

# ── 1. Egress CA at the JVM level ────────────────────────────────────────────
# Bazel's downloader runs in the server JVM, which validates against a Java KeyStore and
# ignores SSL_CERT_FILE. The proxy bumps every host, so a store holding only the egress CA
# validates bcr.bazel.build / GitHub / npm / PyPI alike. The CA bundle is mounted by the
# Kyverno egress injection at $EGRESS_CA. (Adapted from haku-state tools/ci/trust_egress_ca.sh.)
#
# HAKU_SETUP_BAZEL_TRUST=0 for a box with no JVM and no Bazel — the Claude runner image is
# python + git + the claude CLI, and `keytool` is not in it. An explicit switch rather than
# `command -v keytool`, so a haku-sandbox image that lost its JVM still fails loudly here
# instead of silently shipping Bazel fetches that cannot verify TLS.
store="$HOME/egress-truststore.p12"
pass="changeit"
egress_cn="haku-egress-proxy-root-ca"

if [ "${HAKU_SETUP_BAZEL_TRUST:-1}" != "1" ]; then
  echo "haku-sandbox-setup: skipping Bazel JVM truststore (HAKU_SETUP_BAZEL_TRUST=0)"
elif [ -r "$bundle" ]; then
  tmp="$(mktemp -d)"
  csplit -sz -f "$tmp/c" -b '%02d.pem' "$bundle" '/-----BEGIN CERTIFICATE-----/' '{*}'
  rm -f "$store"
  certs=()
  for f in "$tmp"/c*.pem; do
    openssl x509 -noout -subject -in "$f" 2>/dev/null | grep -q "$egress_cn" && certs+=("$f")
  done
  [ "${#certs[@]}" -eq 0 ] && certs=("$tmp"/c*.pem) # fallback: import all (slow, correct)
  for f in "${certs[@]}"; do
    keytool -importcert -noprompt -storepass "$pass" -storetype PKCS12 \
      -keystore "$store" -alias "egress-$(basename "$f" .pem)" -file "$f"
  done
  cat >"$HOME/.bazelrc" <<RC
startup --host_jvm_args=-Djavax.net.ssl.trustStore=$store
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=$pass
startup --host_jvm_args=-Djavax.net.ssl.trustStoreType=PKCS12
build --disk_cache=$HOME/.cache/bazel-disk
RC
else
  echo "haku-sandbox-setup: egress CA bundle not found at $bundle — bazel fetches may fail TLS" >&2
fi

# ── 2. Git identity ──────────────────────────────────────────────────────────
# Without this the first `git commit` of a run dies "Author identity unknown".
git config --global user.name haku
git config --global user.email haku@allegedly.works

# ── 2b. Git TLS trust ────────────────────────────────────────────────────────
# git links against a system OpenSSL that reads neither SSL_CERT_FILE nor CURL_CA_BUNDLE
# (both of which the Kyverno egress injection sets, and which curl does honor). The proxy
# bumps every external host, so without this git sees a cert chain it can't build and dies
# `server certificate verification failed. CAfile: none` — while `curl https://github.com/`
# beside it returns 200, which makes the failure read like a network block rather than a
# trust-store gap. Measured 2026-07-25: setting this is the whole difference between
# `git ls-remote https://github.com/...` failing and returning devel's HEAD.
if [ -r "$bundle" ]; then
  git config --global http.sslCAInfo "$bundle"
fi

# ── 3. Git credentials ───────────────────────────────────────────────────────
# The haku Forgejo account (HAKU_GIT_* from the haku-forgejo-git secret, via the
# SandboxTemplate) reaches the SAME Forgejo under two hostnames, and both are needed:
# forgejo-http.forgejo in-cluster (the ducktape_haku module git_override on every bazel
# invocation + the haku-state clone below) and the public git.allegedly.works, which the
# CLI's REST readers use — `haku read --source cpap` 404s unauthenticated against it.
# (Collapsing the two names is tracked in haku/TODO.md.)
umask 077
cat >"$HOME/.netrc" <<NETRC
machine forgejo-http.forgejo
login ${HAKU_GIT_USERNAME:?HAKU_GIT_USERNAME unset}
password ${HAKU_GIT_PASSWORD:?HAKU_GIT_PASSWORD unset}
machine git.allegedly.works
login ${HAKU_GIT_USERNAME}
password ${HAKU_GIT_PASSWORD}
NETRC

# GitHub, only where the template asked for it. HAKU_GITHUB_TOKEN is a non-secret
# placeholder the egress proxy swaps for the real agentydragon-agent PAT on the way out, so
# what lands here is inert on its own — that is the point, and why this is safe to write
# into a file the agent can read. The login name is ignored by GitHub when the password is a
# token, but git demands one. Absent in the haku-sandbox template, which reaches github.com
# unauthenticated for public reads and should keep doing so.
if [ -n "${HAKU_GITHUB_TOKEN:-}" ]; then
  cat >>"$HOME/.netrc" <<NETRC
machine github.com
login x-access-token
password ${HAKU_GITHUB_TOKEN}
NETRC
fi

# ── 3b. Kubernetes through Console's proxy ───────────────────────────────────
# This box mounts no ServiceAccount token, so `kubectl` reaches Kubernetes only through
# haku-kube-api-proxy, which authorizes every request against Console before forwarding it
# under the proxy's own in-cluster credential. Standing authority is unchanged: Console SARs
# the haku access-profile group, which cluster/k8s/haku/rbac/rolebinding-haku.yaml binds to
# the same haku-sandbox-admin Role the mounted token used to carry. What the box loses is a
# credential it could exfiltrate, and `kubectl attach` / `kubectl proxy`, which the proxy
# answers 501 (exec, port-forward, logs -f and watch all stream).
#
# The bearer goes in a mode-0600 tokenFile rather than inline, mirroring the Claude runner's
# _materialize_proxy_kubeconfig (haku/runner/runner.py). Both conditions must hold,
# which is also what keeps the two writers apart: the runner Pod has the proxy URL from its
# claim env but no HAKU_CONSOLE_TOKEN, so it skips this and materializes its own kubeconfig
# from the exact-session bearer instead.
if [ -n "${HAKU_KUBERNETES_PROXY_URL:-}" ] && [ -n "${HAKU_CONSOLE_TOKEN:-}" ]; then
  kube_dir="$HOME/.kube"
  mkdir -p "$kube_dir"
  chmod 700 "$kube_dir"
  umask 077
  printf '%s' "${HAKU_CONSOLE_TOKEN}" >"$kube_dir/haku-agent-token"
  # The proxy URL is https (client-go attaches kubeconfig credentials only to a TLS server),
  # and its certificate chains to cluster-root-ca, carried in the egress trust bundle.
  kube_ca=""
  if [ -r "$bundle" ]; then
    kube_ca=", \"certificate-authority\": \"$bundle\""
  fi
  # JSON is valid kubeconfig YAML, so the URL is carried as a JSON string rather than as
  # bytes a YAML parser would reinterpret.
  cat >"$kube_dir/config" <<KUBECONFIG
{
  "apiVersion": "v1",
  "kind": "Config",
  "clusters": [{"name": "haku-console-proxy", "cluster": {"server": "${HAKU_KUBERNETES_PROXY_URL}"${kube_ca}}}],
  "users": [{"name": "haku-agent", "user": {"tokenFile": "$kube_dir/haku-agent-token"}}],
  "contexts": [{"name": "haku-agent", "context": {"cluster": "haku-console-proxy", "user": "haku-agent"}}],
  "current-context": "haku-agent"
}
KUBECONFIG
else
  echo "haku-sandbox-setup: no Console Kubernetes proxy configured — leaving kubectl unconfigured"
fi

# ── 4. haku-state checkout ───────────────────────────────────────────────────
# So `bazel run //cli:...` has something to run against. Branch is main (haku-state's
# default), not master.
#
# --depth 1: forgejo's full-history pack generation is the dominant cost (a full clone runs
# minutes even with the egress proxy bypassed; a shallow clone is ~9s), and the sandbox only
# builds/runs the HEAD checkout — it needs no history. Deviation: if a build step ever needs
# git history/tags (e.g. `git describe` version stamping), deepen the fetch here.
repo="${HAKU_STATE_DIR:-/workspace/haku-state}"
url="${HAKU_STATE_URL:-http://forgejo-http.forgejo:3000/haku/haku-state.git}"
if [ -d "$repo/.git" ]; then
  git -C "$repo" fetch --depth 1 --prune origin main
  git -C "$repo" checkout -B main origin/main
  git -C "$repo" reset --hard origin/main
else
  git clone --depth 1 --branch main --single-branch "$url" "$repo"
fi

# ── 5. ducktape checkout (one of Haku's information sources) ─────────────────
# Haku's manual is not here — it lives in the haku-state checkout above. ducktape is cloned
# because its recent history is a source Haku reads for follow-up work the operator may want
# surfaced (haku-state `sources/ducktape.md`), and because the runtime entrypoints live here.
#
# From GitHub, not the in-cluster Forgejo mirror: the mirror is not yet auto-synced and was
# measured 3 commits behind devel (97a23895 vs a4c497f7) on 2026-07-25, so it under-reports
# recent work. Public repo, so no credential — the egress proxy allows github.com and §2b
# just taught git to trust it.
#
# --filter=blob:none, NOT --depth 1: that history read spans weeks,
# which a shallow clone cannot do. A partial clone keeps every commit and fetches blobs on
# demand — measured 11s / ~102 MB, against minutes for a full clone.
if [ "${HAKU_DUCKTAPE_SKIP:-}" != "1" ]; then
  dt_repo="${HAKU_DUCKTAPE_DIR:-/workspace/ducktape}"
  dt_url="${HAKU_DUCKTAPE_URL:-https://github.com/agentydragon/ducktape.git}"
  dt_branch="${HAKU_DUCKTAPE_BRANCH:-devel}"
  # Non-fatal: ducktape is read-only reference. A run that loses this source should
  # degrade to "surface a finding and carry on", not fail to get a sandbox at all.
  if [ -d "$dt_repo/.git" ]; then
    git -C "$dt_repo" fetch --prune origin "$dt_branch" \
      && git -C "$dt_repo" checkout -B "$dt_branch" "origin/$dt_branch" \
      && git -C "$dt_repo" reset --hard "origin/$dt_branch" \
      || echo "haku-sandbox-setup: ducktape refresh failed — that source unavailable this claim" >&2
  else
    git clone --filter=blob:none --single-branch --branch "$dt_branch" "$dt_url" "$dt_repo" \
      || echo "haku-sandbox-setup: ducktape clone failed — that source unavailable this claim" >&2
  fi
fi

echo "haku-sandbox-setup: egress CA trusted, git identity + credentials written, haku-state + ducktape synced"
